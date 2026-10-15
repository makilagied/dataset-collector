"""One stored comment per set of hit words (DEDUPE_HITS).

A comment whose hit words are exactly those of a stored comment ("ujinga mtupu" after
"wewe ujinga") is not stored. The stored one, the original, counts it in `occurrences`,
and its Instagram/Facebook id goes into comment_duplicates so syncing the same post again
does not count it twice. hit_words / hit_roots counts weigh each comment by its
occurrences, so repeats still count there.
"""

from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from collector.models import Comment, CommentDuplicate, CommentFeatures, Sample
from collector.nlp.highlight import hit_key
from collector.pipeline.hitwords import forget_comments

STALE = "stale"  # pipeline_version that makes `process` recount a comment


def is_folded(session: Session, ig_comment_id: str) -> bool:
    return session.get(CommentDuplicate, ig_comment_id) is not None


def original_for(session: Session, key: str, ig_comment_id: str) -> int | None:
    """Id of the stored comment with these hit words, other than this comment itself."""
    if not key:
        return None
    return session.scalars(
        select(Comment.id).where(Comment.hit_key == key, Comment.ig_comment_id != ig_comment_id)
    ).first()


def _recount(session: Session, comment_id: int) -> None:
    session.execute(
        update(CommentFeatures)
        .where(CommentFeatures.comment_id == comment_id)
        .values(pipeline_version=STALE)
    )


def fold(session: Session, original_id: int, ig_comment_id: str, source: str) -> bool:
    """Count a comment that is not stored into its original. False if counted before."""
    added = session.execute(
        insert(CommentDuplicate)
        .values(ig_comment_id=ig_comment_id, comment_id=original_id, source=source)
        .on_conflict_do_nothing()
        .returning(CommentDuplicate.ig_comment_id)
    ).scalar_one_or_none()
    if added is None:
        return False
    session.execute(
        update(Comment).where(Comment.id == original_id).values(occurrences=Comment.occurrences + 1)
    )
    _recount(session, original_id)
    return True


def merge(session: Session, original: Comment, duplicate: Comment) -> None:
    """Fold a stored comment into another and delete it."""
    session.flush()
    session.execute(
        insert(CommentDuplicate)
        .values(
            ig_comment_id=duplicate.ig_comment_id,
            comment_id=original.id,
            source=duplicate.source,
        )
        .on_conflict_do_nothing()
    )
    session.execute(
        update(CommentDuplicate)
        .where(CommentDuplicate.comment_id == duplicate.id)
        .values(comment_id=original.id)
    )
    original.occurrences += duplicate.occurrences
    forget_comments(session, [duplicate.id])
    session.execute(delete(Sample).where(Sample.comment_id == duplicate.id))
    session.execute(delete(CommentFeatures).where(CommentFeatures.comment_id == duplicate.id))
    session.delete(duplicate)
    session.flush()
    _recount(session, original.id)


@dataclass
class DedupeStats:
    kept: int = 0
    merged: int = 0


def dedupe_all(session: Session, *, dry_run: bool = False) -> DedupeStats:
    """Recompute every comment's hit words and merge comments that share them into the
    earliest stored one. Run `process` afterwards to recount."""
    comments = session.scalars(select(Comment).order_by(Comment.id)).all()
    groups: dict[str, list[Comment]] = {}
    for c in comments:
        key = hit_key(c.text_raw or "")
        groups.setdefault(key or f"\0{c.id}", []).append(c)  # no hit words: unique
    stats = DedupeStats(kept=len(groups), merged=len(comments) - len(groups))
    if dry_run:
        return stats

    # Clear old keys first so assigning the new ones never collides mid-way.
    session.execute(update(Comment).values(hit_key=None))
    for key, group in groups.items():
        original, *duplicates = group
        for duplicate in duplicates:
            merge(session, original, duplicate)
        original.hit_key = None if key.startswith("\0") else key
    session.commit()
    return stats
