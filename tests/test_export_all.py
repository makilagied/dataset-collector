import csv
import io
import json
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from collector.demo import load_demo
from collector.ig.backfill import upsert_comment
from collector.models import Media
from collector.nlp.langid import HeuristicDetector
from collector.pipeline.export import (
    ALL_COLUMNS,
    ExportError,
    export_all_comments,
    iter_all_comments,
)
from collector.pipeline.process import process


def read_csv(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


@pytest.fixture
def demo(db: Session) -> Session:
    load_demo(db)
    process(db, detector=HeuristicDetector())
    return db


def test_exports_every_comment_abusive_or_not(demo: Session, tmp_path: Path) -> None:
    result = export_all_comments(demo, "csv", tmp_path / "all.csv")
    rows = read_csv((tmp_path / "all.csv").read_text(encoding="utf-8"))

    assert result.rows == len(rows) == 45  # 36 comments + 9 replies
    assert tuple(rows[0]) == ALL_COLUMNS
    hits = [r for r in rows if r["is_hit"] == "True"]
    clean = [r for r in rows if r["is_hit"] == "False"]
    assert hits and clean and len(hits) + len(clean) == 45
    by_text = {r["text_raw"]: r for r in rows}
    assert by_text["malaya tu huyu"]["matched_roots"] == "malaya"
    assert by_text["habari za asubuhi dada"]["matched_roots"] == ""
    assert by_text["😂😂😂"]["exclude_reason"] == "emoji_only"  # excluded ones included too
    assert by_text["'@USER @USER"]["exclude_reason"] == "mention_only"  # masked + formula guard
    assert sum(r["is_reply"] == "True" for r in rows) == 9


def test_no_identities_in_export(demo: Session, tmp_path: Path) -> None:
    for fmt in ("csv", "jsonl"):
        path = tmp_path / f"all.{fmt}"
        export_all_comments(demo, fmt, path)
        text = path.read_text(encoding="utf-8")
        assert "demo_user_" not in text and "demo_fan_" not in text
        assert "demo-igsid" not in text and "demo-c" not in text  # no IG ids of any kind


@pytest.mark.parametrize(
    ("show", "check"),
    [
        ("hits", lambda rs: rs and all(r["is_hit"] is True for r in rs)),
        ("matched", lambda rs: rs and all(r["matched_roots"] for r in rs)),
        ("excluded", lambda rs: rs and all(r["exclude_reason"] for r in rs)),
        ("unprocessed", lambda rs: rs == []),
    ],
)
def test_filters(demo: Session, show: str, check: object) -> None:
    text = "".join(iter_all_comments(demo, "jsonl", show=show))
    records = [json.loads(line) for line in text.splitlines()]
    assert check(records)  # type: ignore[operator]


def test_unprocessed_comments_are_included(db: Session) -> None:
    acct, _ = load_demo(db)  # not processed
    records = [json.loads(x) for x in "".join(iter_all_comments(db, "jsonl")).splitlines()]
    assert len(records) == 45
    assert all(r["exclude_reason"] == "unprocessed" and r["is_hit"] == "" for r in records)
    assert len("".join(iter_all_comments(db, "jsonl", account_id=acct.id + 99))) == 0


def test_multiline_comments_and_chunking(db: Session, tmp_path: Path) -> None:
    acct, _ = load_demo(db)
    media = db.query(Media).first()
    assert media is not None
    upsert_comment(
        db, media.id, {"id": "ml", "text": "mstari wa kwanza\nmstari wa pili"}, "webhook"
    )
    db.commit()
    result = export_all_comments(db, "csv", tmp_path / "x.csv", chunk=7)
    rows = read_csv((tmp_path / "x.csv").read_text(encoding="utf-8"))
    assert result.rows == len(rows) == 46
    assert any(r["text_raw"] == "mstari wa kwanza\nmstari wa pili" for r in rows)


def test_invalid_options(db: Session) -> None:
    with pytest.raises(ExportError):
        list(iter_all_comments(db, "xlsx"))
    with pytest.raises(ExportError):
        list(iter_all_comments(db, "csv", show="everything"))


def test_cli_export_all(demo: Session, tmp_path: Path) -> None:
    from collector.cli import app

    out = tmp_path / "cli.jsonl"
    result = CliRunner().invoke(app, ["export-all", "--format", "jsonl", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "wrote 45 comments" in result.output
    assert len(out.read_text(encoding="utf-8").splitlines()) == 45
    bad = CliRunner().invoke(app, ["export-all", "--show", "nope", "--out", str(out)])
    assert bad.exit_code == 1


def test_admin_download(demo: Session, engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from collector.api import app, get_sessionmaker
    from collector.config import get_settings

    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    get_settings.cache_clear()
    factory = sessionmaker(engine, expire_on_commit=False)
    app.dependency_overrides[get_sessionmaker] = lambda: factory
    try:
        client = TestClient(app)
        client.auth = ("admin", "pw")
        page = client.get("/admin/comments?show=hits").text
        export_link = "/admin/comments/export?account=&amp;show=hits&amp;q=&amp;media=&amp;root="
        assert export_link + "&amp;fmt=csv" in page

        resp = client.get("/admin/comments/export?fmt=csv&show=all")
        assert resp.status_code == 200
        assert resp.headers["content-disposition"].startswith('attachment; filename="comments-all-')
        assert len(read_csv(resp.content.decode("utf-8"))) == 45

        hits = client.get("/admin/comments/export?fmt=jsonl&show=hits").text.splitlines()
        assert hits and all(json.loads(h)["is_hit"] is True for h in hits)
        assert client.get("/admin/comments/export?fmt=xlsx").status_code == 400
    finally:
        app.dependency_overrides.clear()
