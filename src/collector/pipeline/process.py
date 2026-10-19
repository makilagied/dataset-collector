"""Normalize, match and language-ID comments into comment_features, in batches.

Selects comments with no features row or a row from another pipeline version, walking the
comments table by primary key so the whole table is never loaded at once.
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from collector.models import Comment, CommentFeatures
from collector.nlp import langid
from collector.nlp.highlight import hit_terms
from collector.nlp.lexicon import Lexicon, load_lexicon
from collector.nlp.matcher import match
from collector.nlp.normalize import is_protected, normalize, normalize_masked, normalize_plain
from collector.pipeline.hitwords import CountDelta, apply_counts

# Bump when normalizer, lexicon or matcher behaviour changes to reprocess everything.
# 2: English lexicon, 3: typo-like fuzzy only, 4: verbs + hit_words, 5: mamae, 6: hit_roots,
# 7: words joined by "/" or "." kept apart, highlights split at punctuation, 8: occurrences,
# 9: mninga allowlisted
PIPELINE_VERSION = "9"
_ALNUM = re.compile(r"[^\W_]")


def pipeline_version(detector: langid.Detector | None = None) -> str:
    return f"{PIPELINE_VERSION}+{(detector or langid.get_detector()).name}"


def exclude_reason(text_norm: str) -> str | None:
    """Why a comment is not worth sampling (duplicates are detected separately)."""
    tokens = text_norm.split()
    if not tokens:
        return "empty"
    protected = [t for t in tokens if is_protected(t)]
    rest = [t for t in tokens if not is_protected(t)]
    if not any(_ALNUM.search(t) for t in rest):
        if any(not t.startswith("@") for t in protected):
            return "link_only"
        if protected:
            return "mention_only"
        return "emoji_only"
    return None


@dataclass
class ProcessStats:
    processed: int = 0
    hits: int = 0
    excluded: Counter[str] = field(default_factory=Counter)
    langs: Counter[str] = field(default_factory=Counter)


def _earlier_texts(session: Session, texts: set[str], batch_ids: list[int]) -> dict[str, int]:
    """Lowest comment_id outside this batch already holding each text."""
    if not texts:
        return {}
    # Postgres md5() hashes the UTF-8 bytes, same as hashlib; served by the md5 index.
    hashes = [hashlib.md5(t.encode()).hexdigest() for t in texts]
    rows = session.execute(
        select(CommentFeatures.text_norm, func.min(CommentFeatures.comment_id))
        .where(
            func.md5(CommentFeatures.text_norm).in_(hashes),
            CommentFeatures.comment_id.not_in(batch_ids),
        )
        .group_by(CommentFeatures.text_norm)
    ).all()
    return {text: cid for text, cid in rows if text in texts}


def process(
    session: Session,
    *,
    batch_size: int = 500,
    reprocess_all: bool = False,
    lexicon: Lexicon | None = None,
    detector: langid.Detector | None = None,
) -> ProcessStats:
    lex = lexicon or load_lexicon()
    det = detector or langid.get_detector()
    version = pipeline_version(det)
    stats = ProcessStats()
    last_id = 0

    while True:
        query = (
            select(
                Comment.id,
                Comment.text_raw,
                Comment.occurrences,
                CommentFeatures.hit_words,
                CommentFeatures.hit_roots,
                CommentFeatures.hit_weight,
            )
            .outerjoin(CommentFeatures, CommentFeatures.comment_id == Comment.id)
            .where(Comment.id > last_id)
            .order_by(Comment.id)
            .limit(batch_size)
        )
        if not reprocess_all:
            query = query.where(
                or_(
                    CommentFeatures.comment_id.is_(None),
                    CommentFeatures.pipeline_version != version,
                )
            )
        batch = session.execute(query).all()
        if not batch:
            break
        last_id = batch[-1].id

        normalized = [(cid, raw, normalize(raw or "")) for cid, raw, *_ in batch]
        candidates = {norm for _, _, norm in normalized if norm and exclude_reason(norm) is None}
        earlier = _earlier_texts(session, candidates, [cid for cid, _, _ in normalized])
        seen_in_batch: set[str] = set()

        rows: list[dict[str, Any]] = []
        counts = CountDelta()
        for (cid, raw, norm), (*_, weight, old_words, old_roots, old_weight) in zip(
            normalized, batch, strict=True
        ):
            reason = exclude_reason(norm)
            if reason is None:
                if norm in seen_in_batch or earlier.get(norm, cid) < cid:
                    reason = "duplicate"
                seen_in_batch.add(norm)
            result = match(norm, lex, normalize_plain(raw or ""), normalize_masked(raw or ""))
            lang = det.detect(raw or "")
            terms = hit_terms(raw or "", result)
            counts.change(old_words, old_roots, terms, old_weight or 1, weight)
            rows.append(
                {
                    "comment_id": cid,
                    "text_norm": norm,
                    "exclude_reason": reason,
                    "lang": lang.lang,
                    "lang_conf": lang.conf,
                    "matched_roots": result.matched_roots,
                    "match_methods": result.match_methods,
                    "match_score": result.score,
                    "hit_words": list(terms),
                    "hit_roots": sorted(set(terms.values())),
                    "hit_weight": weight,
                    "pipeline_version": version,
                }
            )
            stats.processed += 1
            stats.hits += result.is_hit
            stats.langs[lang.lang or "unknown"] += 1
            if reason:
                stats.excluded[reason] += 1

        stmt = insert(CommentFeatures).values(rows)
        updates: dict[str, Any] = {k: stmt.excluded[k] for k in rows[0] if k != "comment_id"}
        updates["processed_at"] = func.now()
        session.execute(stmt.on_conflict_do_update(index_elements=["comment_id"], set_=updates))
        apply_counts(session, counts)
        session.commit()

    return stats
