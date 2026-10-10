from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from collector.demo import DEMO_IG_USER_ID, load_demo, remove_demo
from collector.models import Account, Comment, Sample, SyncState
from collector.nlp.langid import HeuristicDetector
from collector.pipeline.export import export_batch
from collector.pipeline.process import process
from collector.pipeline.sample import sample_batch
from collector.pipeline.stats import collect_stats


def count(db: Session, model: type) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def test_demo_load_is_idempotent_and_removable(db: Session) -> None:
    account, stats = load_demo(db)
    assert account.ig_user_id == DEMO_IG_USER_ID
    assert stats.media_seen == 3
    assert stats.comments_inserted == count(db, Comment) == 36 + 9  # comments + replies
    assert count(db, SyncState) == 0

    _, again = load_demo(db)
    assert again.comments_inserted == 0

    assert remove_demo(db) == 45
    assert count(db, Comment) == 0
    assert count(db, Account) == 0
    assert remove_demo(db) == 0


def test_demo_end_to_end_without_identity_leaks(db: Session, tmp_path: Path) -> None:
    load_demo(db)
    process(db, detector=HeuristicDetector())
    sample_batch(db, "demo", 20, seed=1)
    path = tmp_path / "demo.csv"
    export_batch(db, "demo", "csv", path)
    content = path.read_text(encoding="utf-8-sig")
    assert "demo_user_" not in content and "demo_fan_" not in content
    assert "@USER" in content or "demo_fan" not in content


def test_stats(db: Session) -> None:
    assert collect_stats(db).accounts == []
    load_demo(db)
    before = collect_stats(db)
    assert before.comments == 45 and before.processed == 0 and before.hit_rate == 0.0

    process(db, detector=HeuristicDetector())
    sample_batch(db, "b1", 10, seed=2)
    s = collect_stats(db)

    (acct,) = s.accounts
    assert (acct.media, acct.comments, acct.processed) == (3, 45, 45)
    assert acct.category == "demo"
    # the same 3 replies sit under each of the 3 posts (6 duplicates), plus one repeated comment
    assert s.excluded == {"duplicate": 7, "emoji_only": 1, "link_only": 1, "mention_only": 1}
    assert acct.eligible == 45 - 10
    assert 0 < acct.hits < acct.eligible
    assert s.hit_rate == pytest.approx(acct.hits / acct.eligible)
    assert sum(s.languages.values()) == acct.eligible
    (batch,) = s.batches
    assert (batch.name, batch.size, batch.exported) == ("b1", 10, False)
    assert sum(batch.strata.values()) == 10


def test_cli_demo_process_stats(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    """The README's quick-start commands, run through the real CLI."""
    import collector.nlp.langid as langid
    from collector.cli import app

    monkeypatch.setattr(langid, "get_detector", HeuristicDetector)
    runner = CliRunner()
    for args in (["demo", "load"], ["process"], ["stats"]):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
    assert "@demo_account" in result.output and "hit rate" in result.output
    assert count(db, Sample) == 0
    result = runner.invoke(app, ["demo", "remove"])
    assert result.exit_code == 0 and "removed demo account and 45 comments" in result.output
