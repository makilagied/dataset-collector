"""Export a sample batch for labelling as CSV or JSONL.

Personal data is masked in both text columns: @mentions -> @USER, URLs -> <URL> (profile
links carry usernames), emails -> <EMAIL>. Commenter identifiers (even hashed) are never
exported. CSV cells that a spreadsheet would read as a formula get a leading quote.
"""

import csv
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from collector.models import Account, Comment, CommentFeatures, Media, Sample

COLUMNS = (
    "sample_id",
    "text_raw",
    "text_norm",
    "matched_roots",
    "stratum",
    "account_category",
    "commented_at",
    "label",
    "target",
    "annotator",
)
FORMATS = ("csv", "jsonl")

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_MENTION = re.compile(r"(?<![\w@])@[\w.]+")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


class ExportError(ValueError):
    pass


def mask(text: str) -> str:
    text = _EMAIL.sub("<EMAIL>", text)
    text = _URL.sub("<URL>", text)
    return _MENTION.sub("@USER", text)


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


@dataclass
class ExportResult:
    path: Path
    rows: int


def export_batch(session: Session, batch: str, fmt: str, out: Path) -> ExportResult:
    if fmt not in FORMATS:
        raise ExportError(f"format must be one of {FORMATS}")
    rows = session.execute(
        select(
            Sample.id,
            Sample.stratum,
            Comment.text_raw,
            Comment.commented_at,
            CommentFeatures.text_norm,
            CommentFeatures.matched_roots,
            Account.category,
        )
        .join(Comment, Comment.id == Sample.comment_id)
        .join(CommentFeatures, CommentFeatures.comment_id == Comment.id)
        .join(Media, Media.id == Comment.media_id)
        .join(Account, Account.id == Media.account_id)
        .where(Sample.batch_name == batch)
        .order_by(Sample.id)
    ).all()
    if not rows:
        raise ExportError(f"batch {batch!r} not found or empty")

    records = [
        {
            "sample_id": r.id,
            "text_raw": mask(r.text_raw or ""),
            "text_norm": mask(r.text_norm or ""),
            "matched_roots": list(r.matched_roots or []),
            "stratum": r.stratum,
            "account_category": r.category or "",
            "commented_at": r.commented_at.isoformat() if r.commented_at else "",
            "label": "",
            "target": "",
            "annotator": "",
        }
        for r in rows
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        # utf-8-sig so Excel shows Swahili text and emoji correctly.
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=COLUMNS)
            writer.writeheader()
            for rec in records:
                flat = {**rec, "matched_roots": ";".join(rec["matched_roots"])}
                writer.writerow({k: _csv_safe(v) for k, v in flat.items()})
    else:
        with out.open("w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    session.execute(
        update(Sample).where(Sample.batch_name == batch).values(exported_at=datetime.now(UTC))
    )
    session.commit()
    return ExportResult(path=out, rows=len(records))
