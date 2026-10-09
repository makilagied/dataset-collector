import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.accounts import refresh_expiring
from collector.api import app, get_http, get_session
from collector.config import get_settings
from collector.ig import oauth
from collector.models import Account
from collector.security import decrypt_token, encrypt_token

SHORT = "short-lived-token"
LONG = "long-lived-token"


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    def _session() -> Iterator[Session]:
        yield db
        db.commit()

    def _http() -> Iterator[httpx.Client]:
        with httpx.Client() as c:
            yield c

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_http] = _http
    yield TestClient(app)
    app.dependency_overrides.clear()


def mock_instagram(router: respx.MockRouter) -> None:
    router.post(oauth.SHORT_TOKEN_URL).respond(
        json={
            "data": [
                {
                    "access_token": SHORT,
                    "user_id": "999",
                    "permissions": "instagram_business_basic,instagram_business_manage_comments",
                }
            ]
        }
    )
    router.get(f"{oauth.GRAPH_HOST}/access_token").respond(
        json={"access_token": LONG, "token_type": "bearer", "expires_in": 5183944}
    )
    router.get(f"{oauth.GRAPH_HOST}/v25.0/me").respond(
        json={"id": "app-scoped-1", "user_id": "17841400000000001", "username": "duka_la_mama"}
    )


def test_state_round_trip_carries_consent_ref() -> None:
    s = get_settings()
    assert oauth.verify_state(s, oauth.make_state(s, "consent-042")) == "consent-042"


@pytest.mark.parametrize(
    "mangle",
    [
        lambda st: st + "x",
        lambda st: "garbage",
        lambda st: st.split(".")[0] + ".AAAA",
    ],
)
def test_tampered_state_rejected(mangle) -> None:  # type: ignore[no-untyped-def]
    s = get_settings()
    with pytest.raises(oauth.OAuthError):
        oauth.verify_state(s, mangle(oauth.make_state(s)))


def test_expired_state_rejected() -> None:
    s = get_settings()
    old = oauth.make_state(s, now=time.time() - oauth.STATE_MAX_AGE_S - 5)
    with pytest.raises(oauth.OAuthError, match="expired"):
        oauth.verify_state(s, old)


def test_connect_redirects_to_instagram(client: TestClient) -> None:
    resp = client.get("/connect", params={"consent_ref": "c-1"}, follow_redirects=False)
    assert resp.status_code == 302
    url = urlparse(resp.headers["location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == oauth.AUTHORIZE_URL
    q = parse_qs(url.query)
    assert q["client_id"] == ["test-app"]
    assert q["response_type"] == ["code"]
    assert q["scope"] == ["instagram_business_basic,instagram_business_manage_comments"]
    assert oauth.verify_state(get_settings(), q["state"][0]) == "c-1"


@respx.mock
def test_callback_stores_account_with_encrypted_token(client: TestClient, db: Session) -> None:
    mock_instagram(respx.mock)
    state = oauth.make_state(get_settings(), "consent-7")

    resp = client.get("/oauth/callback", params={"code": "abc123#_", "state": state})

    assert resp.status_code == 200, resp.text
    assert "duka_la_mama" in resp.text
    sent = respx.calls[0].request.content.decode()
    assert "code=abc123&" in sent + "&"  # '#_' suffix stripped

    acct = db.scalars(select(Account)).one()
    assert acct.ig_user_id == "17841400000000001"
    assert acct.consent_ref == "consent-7"
    assert LONG not in acct.access_token_enc
    assert decrypt_token(acct.access_token_enc) == LONG
    assert acct.token_expires_at is not None
    assert acct.token_expires_at > datetime.now(UTC) + timedelta(days=59)


@respx.mock
def test_reconnect_updates_existing_account(client: TestClient, db: Session) -> None:
    mock_instagram(respx.mock)
    for _ in range(2):
        state = oauth.make_state(get_settings())
        assert (
            client.get("/oauth/callback", params={"code": "c", "state": state}).status_code == 200
        )
    assert len(db.scalars(select(Account)).all()) == 1


@respx.mock
def test_invalid_state_rejected(client: TestClient, db: Session) -> None:
    route = respx.post(oauth.SHORT_TOKEN_URL)
    resp = client.get("/oauth/callback", params={"code": "abc", "state": "forged.state"})
    assert resp.status_code == 400
    assert not route.called
    assert db.scalars(select(Account)).first() is None


def test_user_denied(client: TestClient) -> None:
    state = oauth.make_state(get_settings())
    resp = client.get("/oauth/callback", params={"state": state, "error": "access_denied"})
    assert resp.status_code == 400


@respx.mock
def test_failed_exchange_returns_502(client: TestClient, db: Session) -> None:
    respx.post(oauth.SHORT_TOKEN_URL).respond(400, json={"error_message": "code expired"})
    state = oauth.make_state(get_settings())
    resp = client.get("/oauth/callback", params={"code": "old", "state": state})
    assert resp.status_code == 502
    assert db.scalars(select(Account)).first() is None


@respx.mock
def test_refresh_only_touches_expiring_tokens(db: Session) -> None:
    now = datetime.now(UTC)
    soon = Account(
        ig_user_id="1",
        username="soon",
        access_token_enc=encrypt_token("old-1"),
        token_expires_at=now + timedelta(days=3),
        active=True,
    )
    later = Account(
        ig_user_id="2",
        username="later",
        access_token_enc=encrypt_token("old-2"),
        token_expires_at=now + timedelta(days=40),
        active=True,
    )
    db.add_all([soon, later])
    db.flush()
    route = respx.get(f"{oauth.GRAPH_HOST}/refresh_access_token").respond(
        json={"access_token": "new-1", "token_type": "bearer", "expires_in": 5183944}
    )

    with httpx.Client() as http:
        results = refresh_expiring(db, http)

    assert [(a.username, err) for a, err in results] == [("soon", None)]
    assert route.call_count == 1
    assert route.calls[0].request.url.params["access_token"] == "old-1"
    assert decrypt_token(soon.access_token_enc) == "new-1"
    assert decrypt_token(later.access_token_enc) == "old-2"
