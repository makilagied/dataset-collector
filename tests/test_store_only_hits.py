# Fixtures imported from other test modules shadow the test parameters of the same name.
# ruff: noqa: F811
"""STORE_ONLY_HITS: every way comments come in stores only lexicon hits."""

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.config import get_settings
from collector.ig.backfill import backfill_account
from collector.models import Account, Comment
from collector.storage_filter import is_hit, should_store
from tests.fake_graph import FakeGraph
from tests.test_backfill import account, client, graph, http  # noqa: F401  (fixtures)
from tests.test_webhook import business_login, comments, post
from tests.test_webhook import client as webhook_client  # noqa: F401  (fixture)


@pytest.fixture
def only_hits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORE_ONLY_HITS", "true")
    get_settings.cache_clear()


def test_should_store_follows_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert should_store("habari za asubuhi")  # tests default to storing everything
    monkeypatch.setenv("STORE_ONLY_HITS", "true")
    get_settings.cache_clear()
    assert should_store("wewe ni mjinga") and should_store("fuck you")
    assert not should_store("habari za asubuhi") and not should_store("we nani")


def test_on_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STORE_ONLY_HITS")
    get_settings.cache_clear()
    assert get_settings().store_only_hits is True


def test_backfill_drops_non_hits(
    db: Session, account: Account, graph: FakeGraph, http: httpx.Client, only_hits: None
) -> None:
    stats = backfill_account(db, client(http), account)
    texts = list(db.scalars(select(Comment.text_raw)))
    assert texts and all(is_hit(t) for t in texts)
    assert stats.comments_dropped > 0
    assert stats.comments_inserted + stats.comments_dropped == stats.comments_seen


def test_webhook_drops_non_hits(
    webhook_client: TestClient, db: Session, account: Account, only_hits: None
) -> None:
    assert post(webhook_client, business_login("c1", text="wewe mjinga")) == 200
    assert post(webhook_client, business_login("c2", text="habari za asubuhi")) == 200
    assert set(comments(db)) == {"c1"}
