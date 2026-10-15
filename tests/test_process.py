from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from collector.ig.backfill import upsert_comment
from collector.models import Account, CommentFeatures, Media
from collector.nlp.langid import HeuristicDetector, LangResult
from collector.pipeline.process import (
    PIPELINE_VERSION,
    exclude_reason,
    pipeline_version,
    process,
)
from collector.security import encrypt_token

DETECTOR = HeuristicDetector()


class RenamedDetector(HeuristicDetector):
    name = "other-backend"


@pytest.fixture
def media(db: Session) -> Media:
    acct = Account(ig_user_id="1", username="a", access_token_enc=encrypt_token("t"), active=True)
    db.add(acct)
    db.flush()
    m = Media(ig_media_id="M1", account_id=acct.id)
    db.add(m)
    db.commit()
    return m


def add(db: Session, media: Media, *texts: str) -> None:
    start = len(db.scalars(select(CommentFeatures.comment_id)).all()) + 1000
    for i, t in enumerate(texts):
        upsert_comment(db, media.id, {"id": f"c{start + i}-{t[:8]}", "text": t}, "backfill")
    db.commit()


def features(db: Session) -> dict[str, CommentFeatures]:
    db.expire_all()
    rows = db.execute(select(CommentFeatures, CommentFeatures.comment_id)).scalars().all()
    from collector.models import Comment

    raw = {c.id: c.text_raw for c in db.scalars(select(Comment))}
    return {raw[f.comment_id]: f for f in rows}


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "empty"),
        ("😂 🔥", "emoji_only"),
        ("!!! ...", "emoji_only"),
        ("https://x.com/a", "link_only"),
        ("@juma https://x.com", "link_only"),
        ("@juma @ali", "mention_only"),
        ("@juma mjinga", None),
        ("habari 😂", None),
    ],
)
def test_exclude_reason(text: str, reason: str | None) -> None:
    assert exclude_reason(text) == reason


def test_process_writes_features(db: Session, media: Media) -> None:
    add(db, media, "Wewe ni M.J.1.N.G.@", "habari za asubuhi dada", "mbwa wangu ni mzuri")
    stats = process(db, detector=DETECTOR)

    assert stats.processed == 3
    assert stats.hits == 1  # "mbwa wangu" matches but has no 2nd-person cue
    f = features(db)
    insult = f["Wewe ni M.J.1.N.G.@"]
    assert insult.text_norm == "wewe ni mjinga"
    assert insult.matched_roots == ["jinga"]
    assert insult.match_methods == ["regex"]
    assert insult.match_score == pytest.approx(0.7)
    assert insult.lang == "sw"
    assert insult.pipeline_version == pipeline_version(DETECTOR)
    assert insult.exclude_reason is None
    assert f["habari za asubuhi dada"].matched_roots == []
    assert f["mbwa wangu ni mzuri"].match_score == pytest.approx(0.35)


def test_exclusions_and_duplicates(db: Session, media: Media) -> None:
    add(
        db,
        media,
        "",
        "😂😂😂",
        "https://example.com",
        "@juma @ali",
        "Mjinga!!!",
        "mjinga!",
        "MJINGA!",
    )
    stats = process(db, detector=DETECTOR)
    f = features(db)
    assert {t: r.exclude_reason for t, r in f.items()} == {
        "": "empty",
        "😂😂😂": "emoji_only",
        "https://example.com": "link_only",
        "@juma @ali": "mention_only",
        "Mjinga!!!": None,  # first occurrence stays eligible
        "mjinga!": "duplicate",
        "MJINGA!": "duplicate",
    }
    assert stats.excluded["duplicate"] == 2
    assert len(f) == 7  # everything is kept in the DB


def test_duplicates_detected_across_batches_and_runs(db: Session, media: Media) -> None:
    add(db, media, "sawa kabisa", "habari", "Sawa kabisa!")
    process(db, batch_size=1, detector=DETECTOR)
    add(db, media, "SAWA KABISA")
    process(db, detector=DETECTOR)
    reasons = {t: r.exclude_reason for t, r in features(db).items()}
    assert reasons["sawa kabisa"] is None
    assert reasons["Sawa kabisa!"] is None  # different text_norm ("sawa kabisa!")
    assert reasons["SAWA KABISA"] == "duplicate"


def test_rerun_only_processes_new_comments(db: Session, media: Media) -> None:
    add(db, media, "habari", "mjinga")
    assert process(db, detector=DETECTOR).processed == 2
    assert process(db, detector=DETECTOR).processed == 0
    add(db, media, "mpumbavu")
    assert process(db, detector=DETECTOR).processed == 1


def test_version_bump_reprocesses(db: Session, media: Media) -> None:
    add(db, media, "habari", "mjinga")
    process(db, detector=DETECTOR)
    before = {t: f.processed_at for t, f in features(db).items()}

    stats = process(db, detector=RenamedDetector())

    assert stats.processed == 2
    after = features(db)
    assert {f.pipeline_version for f in after.values()} == {f"{PIPELINE_VERSION}+other-backend"}
    assert all(after[t].processed_at >= before[t] for t in before)


def test_reprocess_all_flag(db: Session, media: Media) -> None:
    add(db, media, "habari")
    process(db, detector=DETECTOR)
    assert process(db, detector=DETECTOR, reprocess_all=True).processed == 1


def test_runs_in_batches(db: Session, engine: Engine, media: Media) -> None:
    add(db, media, *[f"maoni namba {i}" for i in range(7)])
    limits: list[Any] = []

    def capture(conn: Any, cursor: Any, statement: str, params: Any, *args: Any) -> None:
        if "FROM comments" in statement and "LIMIT" in statement:
            limits.append(params.get("param_1") if isinstance(params, dict) else params)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        stats = process(db, batch_size=3, detector=DETECTOR)
    finally:
        event.remove(engine, "before_cursor_execute", capture)

    assert stats.processed == 7
    assert len(limits) == 4  # 3 + 3 + 1, then an empty batch ends the loop
    assert all(limit == 3 for limit in limits)


class FixedDetector:
    name = "fixed"

    def detect(self, text: str) -> LangResult:
        return LangResult(None, 0.0)


def test_unknown_language_counted(db: Session, media: Media) -> None:
    add(db, media, "xyz")
    stats = process(db, detector=FixedDetector())
    assert stats.langs == {"unknown": 1}


@pytest.fixture(autouse=True)
def _no_real_detector(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # Tests pass a detector explicitly; make sure nothing silently loads lingua.
    import collector.nlp.langid as langid

    monkeypatch.setattr(langid, "get_detector", lambda: DETECTOR)
    yield
