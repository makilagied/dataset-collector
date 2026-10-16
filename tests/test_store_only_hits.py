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
from collector.storage_filter import (
    EXCERPT_CHARS,
    MAX_TEXT,
    is_hit,
    prepare,
    should_store,
    text_to_store,
)
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


LONG_POST = (
    "Hakuna cha shamba la bibi wala nini, kwa mfano mimi sijanunua na sitanunua kwakuwa siyo "
    "mambo yangu, fursa vijana tutazame fursa za kutengeneza pesa, Unadhani kina niffer "
    "walikuwa wajinga kulishikilia jambo la miss grand? walijua wanavyoliongelea wanakusanya "
    "kijiji, wanapata followers wapya pia, na biashara zao zinajitangaza"
)


def test_long_comment_keeps_only_an_excerpt_around_the_hit_word() -> None:
    assert len(LONG_POST) > MAX_TEXT
    short = text_to_store(LONG_POST)
    assert short is not None and len(short) <= EXCERPT_CHARS
    assert "wajinga" in short and short in LONG_POST
    assert short == short.strip() and LONG_POST[LONG_POST.index(short) - 1] == " "  # whole words


def test_excerpt_keeps_the_cue_that_makes_an_animal_word_a_hit() -> None:
    text = "habari " * 40 + "wewe ni mbwa kabisa" + " sana" * 40
    short = text_to_store(text)
    assert short is not None and "wewe ni mbwa" in short and is_hit(short)


def test_short_comments_are_kept_whole_and_long_non_hits_dropped() -> None:
    assert text_to_store("wewe ni mjinga") == "wewe ni mjinga"
    assert text_to_store("habari za asubuhi " * 20) is None  # long, no hit word


def test_prepare_records_the_original_length() -> None:
    item = {"id": "1", "text": LONG_POST}
    stored = prepare(item)
    assert stored is not None and stored["text"] != LONG_POST
    assert stored["original_text_length"] == len(LONG_POST)
    assert prepare({"id": "2", "text": "wewe ni mjinga"}) == {"id": "2", "text": "wewe ni mjinga"}


def test_trim_long_comments_cuts_or_deletes_stored_rows(db: Session, account: Account) -> None:
    from collector.ig.backfill import upsert_comment
    from collector.models import CommentFeatures, HitWord, Media
    from collector.nlp.langid import HeuristicDetector
    from collector.pipeline.process import process
    from collector.pipeline.trim import trim_long_comments

    media = Media(ig_media_id="M1", account_id=account.id)
    db.add(media)
    db.flush()
    for cid, text in [("a", LONG_POST), ("b", "habari za asubuhi " * 20), ("c", "mjinga wewe")]:
        upsert_comment(db, media.id, {"id": cid, "text": text}, "backfill")
    db.commit()
    process(db, detector=HeuristicDetector())

    assert trim_long_comments(db, dry_run=True).trimmed == 1
    assert len(db.scalars(select(Comment)).all()) == 3  # dry run changed nothing

    stats = trim_long_comments(db)
    assert (stats.trimmed, stats.deleted) == (1, 1)
    db.expire_all()
    by_id = {c.ig_comment_id: c for c in db.scalars(select(Comment))}
    assert set(by_id) == {"a", "c"}
    assert (
        len(by_id["a"].text_raw) <= EXCERPT_CHARS
        and by_id["a"].raw_json["text"] == by_id["a"].text_raw
    )
    assert by_id["a"].raw_json["original_text_length"] == len(LONG_POST)

    process(db, detector=HeuristicDetector())
    assert len(db.scalars(select(CommentFeatures)).all()) == 2
    counts = {w.word: w.count for w in db.scalars(select(HitWord))}
    assert counts == {"wajinga": 1, "mjinga": 1}  # trimming didn't double count


def test_prune_deletes_stored_comments_that_are_no_longer_hits(
    db: Session, account: Account, only_hits: None
) -> None:
    from collector.ig.backfill import upsert_comment
    from collector.models import Media
    from collector.pipeline.trim import prune_non_hits

    media = Media(ig_media_id="M1", account_id=account.id)
    db.add(media)
    db.flush()
    for cid, text in [("a", "milango ya MNINGA"), ("b", "wewe ni mjinga")]:
        upsert_comment(db, media.id, {"id": cid, "text": text}, "backfill")
    db.commit()

    assert prune_non_hits(db, dry_run=True).deleted == 1
    assert prune_non_hits(db).deleted == 1
    db.expire_all()
    assert list(db.scalars(select(Comment.text_raw))) == ["wewe ni mjinga"]


def test_per_run_override(only_hits: None) -> None:
    long = "habari " * 60
    assert text_to_store("habari za asubuhi", only_hits=False) == "habari za asubuhi"
    assert text_to_store(long, only_hits=False) == long  # kept whole
    assert prepare({"text": long}, only_hits=False) == {"text": long}
    assert text_to_store("habari za asubuhi") is None  # None follows the setting


def test_per_run_only_hits_beats_setting() -> None:
    assert should_store("habari za asubuhi")  # tests default to storing everything
    assert text_to_store("habari za asubuhi", only_hits=True) is None
    assert text_to_store("wewe ni mjinga", only_hits=True) == "wewe ni mjinga"
