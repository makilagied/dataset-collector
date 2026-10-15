"""DEDUPE_HITS: one stored comment per set of hit words; repeats raise its occurrences."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collector.config import get_settings
from collector.ig.backfill import upsert_comment
from collector.models import Account, Comment, CommentDuplicate, HitRoot, HitWord, Media
from collector.nlp.highlight import hit_key
from collector.nlp.langid import HeuristicDetector
from collector.pipeline.dedupe import dedupe_all
from collector.pipeline.process import process
from collector.security import encrypt_token

DETECTOR = HeuristicDetector()


@pytest.fixture
def media(db: Session) -> Media:
    acct = Account(ig_user_id="1", username="a", access_token_enc=encrypt_token("t"), active=True)
    db.add(acct)
    db.flush()
    m = Media(ig_media_id="M1", account_id=acct.id)
    db.add(m)
    db.commit()
    return m


@pytest.fixture
def dedupe_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEDUPE_HITS", "true")
    get_settings.cache_clear()


def store(db: Session, media: Media, ig_id: str, text: str) -> bool:
    inserted = upsert_comment(db, media.id, {"id": ig_id, "text": text}, "scrape")
    db.commit()
    return inserted


def stored(db: Session) -> dict[str, int]:
    db.expire_all()
    return {c.text_raw: c.occurrences for c in db.scalars(select(Comment))}


def test_key_is_the_set_of_hit_words() -> None:
    assert hit_key("wewe ujinga") == hit_key("UJINGA mtupu huu") == "ujinga"
    assert hit_key("mjinga na mpumbavu") == hit_key("mpumbavu, mjinga!")
    assert hit_key("mjinga") != hit_key("wajinga")
    assert hit_key("habari za asubuhi") == ""


def test_repeat_raises_the_original_count_instead_of_storing(
    db: Session, media: Media, dedupe_on: None
) -> None:
    assert store(db, media, "c1", "wewe ujinga")
    assert not store(db, media, "c2", "ujinga mtupu huu")
    assert not store(db, media, "c3", "UJINGA tu")
    assert store(db, media, "c4", "ujinga na mpumbavu")  # another set of hit words
    assert stored(db) == {"wewe ujinga": 3, "ujinga na mpumbavu": 1}

    # syncing the same comments again counts nothing twice
    assert not store(db, media, "c2", "ujinga mtupu huu")
    assert not store(db, media, "c1", "wewe ujinga")
    assert stored(db) == {"wewe ujinga": 3, "ujinga na mpumbavu": 1}


def test_hit_word_counts_include_the_repeats(db: Session, media: Media, dedupe_on: None) -> None:
    store(db, media, "c1", "wewe ujinga")
    process(db, detector=DETECTOR)
    store(db, media, "c2", "ujinga mtupu")  # folded after the original was processed
    store(db, media, "c3", "ujinga huu")
    process(db, detector=DETECTOR)
    db.expire_all()
    assert db.get(HitWord, "ujinga").count == 3
    assert db.get(HitRoot, "jinga").count == 3

    process(db, detector=DETECTOR, reprocess_all=True)
    db.expire_all()
    assert db.get(HitRoot, "jinga").count == 3


def test_dedupe_all_merges_stored_duplicates_into_the_earliest(db: Session, media: Media) -> None:
    for i, text in enumerate(["wewe ujinga", "habari", "ujinga mtupu", "hello", "ujinga tu"]):
        store(db, media, f"c{i}", text)  # DEDUPE_HITS off: all stored
    process(db, detector=DETECTOR)
    assert len(stored(db)) == 5

    assert dedupe_all(db, dry_run=True).merged == 2
    assert len(stored(db)) == 5

    stats = dedupe_all(db)
    assert (stats.kept, stats.merged) == (3, 2)
    assert stored(db) == {"wewe ujinga": 3, "habari": 1, "hello": 1}
    assert db.scalar(select(func.count()).select_from(CommentDuplicate)) == 2

    process(db, detector=DETECTOR)
    db.expire_all()
    assert db.get(HitWord, "ujinga").count == 3
