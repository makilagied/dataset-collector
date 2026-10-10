"""Export comments as CSV or JSONL: a sampled labelling batch, or all comments.

Personal data is masked in both text columns: @mentions -> @USER, URLs -> <URL> (profile
links carry usernames), emails -> <EMAIL>. Commenter identifiers (even hashed) and
Instagram comment IDs (which can be looked up to find the commenter) are never exported.
CSV cells that a spreadsheet would read as a formula get a leading quote.
"""

import csv
import io
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.orm import Session

from collector.models import Account, Comment, CommentFeatures, Media, Sample
from collector.pipeline.sample import HIT_THRESHOLD

SHOW_FILTERS = ("all", "hits", "matched", "excluded", "unprocessed")
ALL_COLUMNS = (
    "comment_id",
    "account",
    "account_category",
    "commented_at",
    "text_raw",
    "text_norm",
    "lang",
    "lang_conf",
    "matched_roots",
    "match_score",
    "is_hit",
    "exclude_reason",
    "is_reply",
    "source",
    "label",
    "target",
    "annotator",
)

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


def render_batch(session: Session, batch: str, fmt: str) -> tuple[str, int]:
    """Masked export of a batch as text in `fmt`; marks the batch exported."""
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

    buf = io.StringIO()
    if fmt == "csv":
        writer = csv.DictWriter(buf, fieldnames=COLUMNS)
        writer.writeheader()
        for rec in records:
            flat = {**rec, "matched_roots": ";".join(rec["matched_roots"])}
            writer.writerow({k: _csv_safe(v) for k, v in flat.items()})
    else:
        for rec in records:
            buf.write(json.dumps(rec, ensure_ascii=False) + "\n")

    session.execute(
        update(Sample).where(Sample.batch_name == batch).values(exported_at=datetime.now(UTC))
    )
    session.commit()
    return buf.getvalue(), len(records)


def file_encoding(fmt: str) -> str:
    # utf-8-sig for CSV so Excel shows Swahili text and emoji correctly.
    return "utf-8-sig" if fmt == "csv" else "utf-8"


def export_batch(session: Session, batch: str, fmt: str, out: Path) -> ExportResult:
    text, rows = render_batch(session, batch, fmt)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding=file_encoding(fmt)) as f:
        f.write(text)
    return ExportResult(path=out, rows=rows)


# --- all comments -------------------------------------------------------------------


def filter_comments[*Ts](
    query: Select[*Ts], account_id: int | None = None, show: str = "all", q: str = ""
) -> Select[*Ts]:
    """Filters shared by the admin Comments page and the all-comments export. The query
    must already join Comment, Media, Account and (outer) CommentFeatures."""
    if show not in SHOW_FILTERS:
        raise ExportError(f"show must be one of {SHOW_FILTERS}")
    if account_id:
        query = query.where(Account.id == account_id)
    if q.strip():
        query = query.where(Comment.text_raw.ilike(f"%{q.strip()}%"))
    if show == "hits":
        query = query.where(
            CommentFeatures.match_score >= HIT_THRESHOLD, CommentFeatures.exclude_reason.is_(None)
        )
    elif show == "matched":
        query = query.where(func.cardinality(CommentFeatures.matched_roots) > 0)
    elif show == "excluded":
        query = query.where(CommentFeatures.exclude_reason.is_not(None))
    elif show == "unprocessed":
        query = query.where(CommentFeatures.comment_id.is_(None))
    return query


def _all_comments_query(account_id: int | None, show: str, q: str) -> Select[*tuple[Any, ...]]:
    query = (
        select(
            Comment.id,
            Comment.text_raw,
            Comment.commented_at,
            Comment.parent_ig_comment_id,
            Comment.source,
            Account.username,
            Account.category,
            CommentFeatures.text_norm,
            CommentFeatures.lang,
            CommentFeatures.lang_conf,
            CommentFeatures.matched_roots,
            CommentFeatures.match_score,
            CommentFeatures.exclude_reason,
        )
        .join(Media, Media.id == Comment.media_id)
        .join(Account, Account.id == Media.account_id)
        .outerjoin(CommentFeatures, CommentFeatures.comment_id == Comment.id)
    )
    return filter_comments(query, account_id, show, q).order_by(Comment.id)


def _record(r: Any) -> dict[str, Any]:
    processed = r.text_norm is not None
    return {
        "comment_id": r.id,  # internal id, not the Instagram comment id
        "account": r.username or "",
        "account_category": r.category or "",
        "commented_at": r.commented_at.isoformat() if r.commented_at else "",
        "text_raw": mask(r.text_raw or ""),
        "text_norm": mask(r.text_norm or ""),
        "lang": r.lang or "",
        "lang_conf": r.lang_conf if r.lang_conf is not None else "",
        "matched_roots": list(r.matched_roots or []),
        "match_score": r.match_score if r.match_score is not None else "",
        "is_hit": (
            bool(
                r.match_score is not None
                and r.match_score >= HIT_THRESHOLD
                and not r.exclude_reason
            )
            if processed
            else ""
        ),
        "exclude_reason": r.exclude_reason or ("" if processed else "unprocessed"),
        "is_reply": r.parent_ig_comment_id is not None,
        "source": r.source,
        "label": "",
        "target": "",
        "annotator": "",
    }


def iter_all_comments(
    session: Session,
    fmt: str,
    *,
    account_id: int | None = None,
    show: str = "all",
    q: str = "",
    chunk: int = 1000,
    counter: list[int] | None = None,
) -> Iterator[str]:
    """Yield the export text piece by piece, reading the table in chunks.
    `counter`, if given, receives the number of rows written."""
    if fmt not in FORMATS:
        raise ExportError(f"format must be one of {FORMATS}")
    query = _all_comments_query(account_id, show, q).execution_options(yield_per=chunk)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=ALL_COLUMNS)
    if fmt == "csv":
        buf.write("﻿")  # BOM so Excel shows Swahili text and emoji correctly
        writer.writeheader()
    for i, row in enumerate(session.execute(query), start=1):
        if counter is not None:
            counter[0] = i
        rec = _record(row)
        if fmt == "csv":
            flat = {**rec, "matched_roots": ";".join(rec["matched_roots"])}
            writer.writerow({k: _csv_safe(v) for k, v in flat.items()})
        else:
            buf.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if i % chunk == 0:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
    if buf.tell():
        yield buf.getvalue()


def export_all_comments(session: Session, fmt: str, out: Path, **filters: Any) -> ExportResult:
    out.parent.mkdir(parents=True, exist_ok=True)
    counter = [0]
    with out.open("w", newline="", encoding="utf-8") as f:
        for piece in iter_all_comments(session, fmt, counter=counter, **filters):
            f.write(piece)
    return ExportResult(path=out, rows=counter[0])
