import csv
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from collector.ig.backfill import backfill_account, upsert_comment
from collector.ig.client import GraphClient
from collector.models import Account, Media, Sample
from collector.nlp.langid import HeuristicDetector
from collector.pipeline.export import COLUMNS, ExportError, export_batch, mask
from collector.pipeline.process import process
from collector.pipeline.sample import sample_batch
from collector.security import encrypt_token
from tests.fake_graph import LEAKY, FakeGraph

TEXTS = [
    "@juma_k wewe ni mjinga",
    "angalia https://instagram.com/some.user mjinga",
    "niandikie juma@mail.co.tz",
    "=1+1 habari",
    "habari za asubuhi",
]


@pytest.fixture
def account(db: Session) -> Account:
    acct = Account(
        ig_user_id="1",
        username="duka",
        access_token_enc=encrypt_token("TOKEN"),
        active=True,
        category="udaku",
    )
    db.add(acct)
    db.commit()
    return acct


@pytest.fixture
def batch(db: Session, account: Account) -> str:
    media = Media(ig_media_id="M1", account_id=account.id)
    db.add(media)
    db.flush()
    for i, t in enumerate(TEXTS):
        upsert_comment(
            db,
            media.id,
            {"id": f"c{i}", "text": t, "timestamp": "2026-09-02T10:00:00+0000"},
            "backfill",
        )
    db.commit()
    process(db, detector=HeuristicDetector())
    sample_batch(db, "b1", 100, (0, 0, 100), seed=3)
    return "b1"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


@pytest.mark.parametrize(
    ("raw", "masked"),
    [
        ("@juma_k mjinga", "@USER mjinga"),
        ("asante @a.b.c na @d", "asante @USER na @USER"),
        ("see https://instagram.com/x?y=1 now", "see <URL> now"),
        ("www.site.co.tz/juma", "<URL>"),
        ("mail juma.k@gmail.com", "mail <EMAIL>"),
        ("m@l@y@", "m@l@y@"),  # leetspeak, not a mention
    ],
)
def test_mask(raw: str, masked: str) -> None:
    assert mask(raw) == masked


def test_csv_export(db: Session, batch: str, tmp_path: Path) -> None:
    result = export_batch(db, batch, "csv", tmp_path / "b1.csv")
    rows = read_csv(result.path)

    assert result.rows == len(TEXTS) == len(rows)
    assert tuple(rows[0]) == COLUMNS
    by_text = {r["text_raw"]: r for r in rows}
    # a leading "@USER" would be read as a formula by spreadsheets
    insult = by_text["'@USER wewe ni mjinga"]
    assert insult["text_norm"] == "'@USER wewe ni mjinga"
    assert insult["matched_roots"] == "jinga"
    assert insult["account_category"] == "udaku"
    assert insult["stratum"] == "random"
    assert insult["commented_at"].startswith("2026-09-02T10:00:00")
    assert (insult["label"], insult["target"], insult["annotator"]) == ("", "", "")
    assert "angalia <URL> mjinga" in by_text
    assert "niandikie <EMAIL>" in by_text
    assert "'=1+1 habari" in by_text
    assert [int(r["sample_id"]) for r in rows] == sorted(int(r["sample_id"]) for r in rows)


def test_jsonl_export(db: Session, batch: str, tmp_path: Path) -> None:
    path = tmp_path / "out" / "b1.jsonl"
    export_batch(db, batch, "jsonl", path)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == len(TEXTS)
    assert set(records[0]) == set(COLUMNS)
    insult = next(r for r in records if "mjinga" in r["text_raw"] and "@USER" in r["text_raw"])
    assert insult["text_raw"] == "@USER wewe ni mjinga"  # no formula guard needed in JSON
    assert insult["matched_roots"] == ["jinga"]


def test_export_marks_batch_exported(db: Session, batch: str, tmp_path: Path) -> None:
    export_batch(db, batch, "csv", tmp_path / "b1.csv")
    db.expire_all()
    assert all(db.scalars(select(Sample.exported_at)))


def test_unknown_batch_and_format(db: Session, batch: str, tmp_path: Path) -> None:
    with pytest.raises(ExportError, match="not found"):
        export_batch(db, "nope", "csv", tmp_path / "x.csv")
    with pytest.raises(ExportError, match="format"):
        export_batch(db, batch, "xlsx", tmp_path / "x.xlsx")


@pytest.fixture
def graph() -> Iterator[FakeGraph]:
    fake = FakeGraph()
    with respx.mock:
        respx.get(url__startswith="https://graph.instagram.com/").mock(side_effect=fake)
        yield fake


@pytest.mark.parametrize("fmt", ["csv", "jsonl"])
def test_end_to_end_export_has_no_commenter_identity(
    db: Session, account: Account, graph: FakeGraph, tmp_path: Path, fmt: str
) -> None:
    """Backfill (usernames in payloads and an @mention in text) -> process -> sample ->
    export. The commenter username must not appear anywhere in the file."""
    with httpx.Client() as http:
        client = GraphClient("TOKEN", http=http, version="v25.0", sleep=lambda s: None)
        backfill_account(db, client, account)
    process(db, detector=HeuristicDetector())
    sample_batch(db, "e2e", 100, (0, 0, 100))
    path = tmp_path / f"e2e.{fmt}"
    export_batch(db, "e2e", fmt, path)

    content = path.read_text(encoding="utf-8-sig")
    assert LEAKY not in content
    assert "igsid-" not in content  # commenter ids, hashed or not, are never exported
    assert "@USER" in content  # the mention in c3 was masked
