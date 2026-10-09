from collections.abc import Iterator
from datetime import date

import httpx
import pytest
import respx
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from collector.ig.backfill import backfill_account
from collector.ig.client import GraphClient, RetryableError
from collector.models import Account, Base, Comment, Media, SyncState
from collector.security import encrypt_token, hash_author
from tests.fake_graph import LEAKY, FakeGraph

ALL_IDS = {"c1", "c2", "c3", "c4", "c5", "c6", "r1", "r2"}


@pytest.fixture
def account(db: Session) -> Account:
    acct = Account(
        ig_user_id="17841400000000001",
        username="duka_la_mama",
        access_token_enc=encrypt_token("TOKEN"),
        active=True,
    )
    db.add(acct)
    db.commit()
    return acct


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    fake = FakeGraph()
    with respx.mock:
        respx.get(url__startswith="https://graph.instagram.com/").mock(side_effect=fake)
        yield fake


@pytest.fixture
def http() -> Iterator[httpx.Client]:
    with httpx.Client() as c:
        yield c


def client(http: httpx.Client, attempts: int = 3) -> GraphClient:
    return GraphClient(
        "TOKEN", http=http, version="v25.0", max_attempts=attempts, sleep=lambda s: None
    )


def comment_ids(db: Session) -> set[str]:
    return set(db.scalars(select(Comment.ig_comment_id)))


def test_full_ingest(db: Session, account: Account, graph: FakeGraph, http: httpx.Client) -> None:
    stats = backfill_account(db, client(http), account)

    assert comment_ids(db) == ALL_IDS
    assert stats.comments_inserted == len(ALL_IDS)
    assert db.scalar(select(func.count()).select_from(Media)) == 2
    # all three M1 comment pages were fetched
    m1_afters = [
        r.url.params.get("after") for r in graph.requests if r.url.path.endswith("M1/comments")
    ]
    assert m1_afters == [None, "p1", "p2"]
    r2 = db.scalars(select(Comment).where(Comment.ig_comment_id == "r2")).one()
    assert r2.parent_ig_comment_id == "c1"
    assert r2.author_hash == hash_author("id:igsid-r2")
    assert db.scalar(select(func.count()).select_from(SyncState)) == 0  # completed run clears state


def test_rerun_inserts_nothing(
    db: Session, account: Account, graph: FakeGraph, http: httpx.Client
) -> None:
    backfill_account(db, client(http), account)
    stats = backfill_account(db, client(http), account)
    assert stats.comments_inserted == 0
    # r1 is seen twice: inline in c1's replies and again via the /replies edge.
    assert stats.comments_seen == len(ALL_IDS) + 1
    assert db.scalar(select(func.count()).select_from(Comment)) == len(ALL_IDS)


def test_interrupted_run_resumes_from_cursor(
    db: Session, account: Account, graph: FakeGraph, http: httpx.Client
) -> None:
    graph.fail_on = {("M1/comments", "p2")}
    with pytest.raises(RetryableError):
        backfill_account(db, client(http), account)

    assert comment_ids(db) == {"c1", "c2", "c3", "c4", "r1", "r2"}
    cursor = db.get(SyncState, f"backfill:{account.id}:comments:M1")
    assert cursor is not None
    assert "after=p2" in cursor.value["next"]
    assert "access_token" not in cursor.value["next"]  # never persist the token

    graph.fail_on.clear()
    graph.requests.clear()
    stats = backfill_account(db, client(http), account)

    m1_afters = [
        r.url.params.get("after") for r in graph.requests if r.url.path.endswith("M1/comments")
    ]
    assert m1_afters == ["p2"]  # resumed, did not restart from page 1
    assert comment_ids(db) == ALL_IDS
    assert stats.comments_inserted == 2  # c5 and c6
    assert db.scalar(select(func.count()).select_from(SyncState)) == 0


def test_since_limits_media(
    db: Session, account: Account, graph: FakeGraph, http: httpx.Client
) -> None:
    backfill_account(db, client(http), account, since=date(2026, 6, 1))
    assert set(db.scalars(select(Media.ig_media_id))) == {"M1"}
    assert "c6" not in comment_ids(db)


def test_no_username_persisted_anywhere(
    db: Session, account: Account, graph: FakeGraph, http: httpx.Client
) -> None:
    """Scan every column of every table (row cast to text) for the test username."""
    backfill_account(db, client(http), account)
    leaks = {}
    for table in Base.metadata.sorted_tables:
        n = db.scalar(
            text(f'SELECT count(*) FROM "{table.name}" t WHERE t::text ILIKE :p'),
            {"p": f"%{LEAKY}%"},
        )
        if table.name == "comments":
            # The only allowed occurrence: an @mention inside comment text (masked at export, M9).
            n = db.scalar(
                text(
                    "SELECT count(*) FROM comments t WHERE t::text ILIKE :p "
                    "AND replace(t::text, text_raw, '') ILIKE :p"
                ),
                {"p": f"%{LEAKY}%"},
            )
        if n:
            leaks[table.name] = n
    assert leaks == {}
    raw = db.scalars(select(Comment.raw_json)).all()
    assert all("from" not in r and "username" not in r for r in raw if r)
