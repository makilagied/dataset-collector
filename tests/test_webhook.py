import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from collector.api import app, get_sessionmaker
from collector.ig.backfill import upsert_comment
from collector.models import Account, Comment, Media
from collector.security import encrypt_token, hash_author

IG_USER = "17841400000000001"
SECRET = "test-secret"  # IG_APP_SECRET in conftest


@pytest.fixture
def client(engine: Engine, db: Session) -> Iterator[TestClient]:
    factory = sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[get_sessionmaker] = lambda: factory
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def account(db: Session) -> Account:
    acct = Account(
        ig_user_id=IG_USER,
        username="duka_la_mama",
        access_token_enc=encrypt_token("T"),
        active=True,
    )
    db.add(acct)
    db.commit()
    return acct


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(
    client: TestClient, payload: dict[str, Any], secret: str = SECRET, signature: str | None = None
) -> int:
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature if signature is not None else sign(body, secret),
    }
    return client.post("/webhook", content=body, headers=headers).status_code


def business_login(
    cid: str,
    text: str = "wewe mjinga",
    field: str = "comments",
    ig_user: str = IG_USER,
    **value: Any,
) -> dict[str, Any]:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": ig_user,
                "time": 1791000000,
                "field": field,
                "value": {
                    "id": cid,
                    "from": {"id": f"igsid-{cid}", "username": "leaky_user"},
                    "text": text,
                    "media": {"id": "M9", "media_product_type": "FEED"},
                    **value,
                },
            }
        ],
    }


def facebook_login(cid: str, field: str = "comments", **value: Any) -> dict[str, Any]:
    return {
        "object": "instagram",
        "entry": [
            {
                "id": IG_USER,
                "time": 1791000000,
                "changes": [
                    {
                        "field": field,
                        "value": {
                            "comment_id": cid,
                            "from": {"id": f"igsid-{cid}", "username": "leaky_user"},
                            "text": "acha ujinga",
                            "media": {"id": "M9", "media_product_type": "REELS"},
                            **value,
                        },
                    }
                ],
            }
        ],
    }


def comments(db: Session) -> dict[str, Comment]:
    db.expire_all()
    return {c.ig_comment_id: c for c in db.scalars(select(Comment))}


# --- verification handshake --------------------------------------------------


def test_verify_handshake(client: TestClient) -> None:
    resp = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "1158201444",
        },
    )
    assert resp.status_code == 200
    assert resp.text == "1158201444"


def test_verify_wrong_token(client: TestClient) -> None:
    resp = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "nope", "hub.challenge": "1"},
    )
    assert resp.status_code == 403


# --- signature ---------------------------------------------------------------


@pytest.mark.parametrize("signature", ["", "sha256=deadbeef", "md5=abc"])
def test_bad_signature_rejected(
    client: TestClient, db: Session, account: Account, signature: str
) -> None:
    assert post(client, business_login("c1"), signature=signature) == 403
    assert comments(db) == {}


def test_wrong_secret_rejected(client: TestClient, db: Session, account: Account) -> None:
    assert post(client, business_login("c1"), secret="other-secret") == 403
    assert comments(db) == {}


def test_fb_app_secret_accepted_when_configured(
    client: TestClient, db: Session, account: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collector.config import get_settings

    monkeypatch.setenv("FB_APP_SECRET", "fb-secret")
    get_settings.cache_clear()
    assert post(client, facebook_login("c1"), secret="fb-secret") == 200
    assert "c1" in comments(db)


# --- ingestion ---------------------------------------------------------------


def test_business_login_payload_ingested(client: TestClient, db: Session, account: Account) -> None:
    assert post(client, business_login("c1")) == 200
    c = comments(db)["c1"]
    assert c.source == "webhook"
    assert c.text_raw == "wewe mjinga"
    assert c.author_hash == hash_author("id:igsid-c1")
    assert c.commented_at is not None
    assert c.raw_json is not None
    assert "from" not in c.raw_json and "leaky_user" not in json.dumps(c.raw_json)
    media = db.scalars(select(Media).where(Media.id == c.media_id)).one()
    assert media.ig_media_id == "M9" and media.account_id == account.id


def test_facebook_login_payload_ingested(client: TestClient, db: Session, account: Account) -> None:
    assert post(client, facebook_login("c2", parent_id="c1")) == 200
    c = comments(db)["c2"]
    assert c.text_raw == "acha ujinga"
    assert c.parent_ig_comment_id == "c1"


def test_live_comments_ingested(client: TestClient, db: Session, account: Account) -> None:
    assert post(client, business_login("l1", field="live_comments")) == 200
    assert post(client, facebook_login("l2", field="live_comments")) == 200
    got = comments(db)
    assert {"l1", "l2"} <= set(got)
    assert got["l1"].raw_json and got["l1"].raw_json["webhook_field"] == "live_comments"


def test_duplicates_ignored(client: TestClient, db: Session, account: Account) -> None:
    for _ in range(3):
        assert post(client, business_login("c1")) == 200
    assert len(comments(db)) == 1


def test_webhook_does_not_clobber_backfilled_comment(
    client: TestClient, db: Session, account: Account
) -> None:
    media = Media(ig_media_id="M9", account_id=account.id)
    db.add(media)
    db.flush()
    upsert_comment(
        db,
        media.id,
        {"id": "c1", "text": "original", "like_count": 12, "hidden": False},
        "backfill",
    )
    db.commit()
    assert post(client, business_login("c1", text="different")) == 200
    c = comments(db)["c1"]
    assert (c.source, c.text_raw, c.like_count) == ("backfill", "original", 12)


def test_unknown_account_and_other_fields_skipped(
    client: TestClient, db: Session, account: Account
) -> None:
    assert post(client, business_login("x1", ig_user="not-connected")) == 200
    assert post(client, business_login("x2", field="mentions")) == 200
    assert comments(db) == {}
