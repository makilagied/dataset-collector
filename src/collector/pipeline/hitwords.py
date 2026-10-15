"""Keep the hit_roots and hit_words tables in step with comment_features.

hit_roots counts each lexicon root once per comment, whatever spelling was used:
"mjinga", "wajinga" and "ujinga" are all "jinga". hit_words breaks that down by spelling.
A comment weighs its `occurrences`: repeats that were folded into it count too.

Counts are changed by deltas: processing a comment adds its new roots/words and takes away
the ones it had before, so reprocessing never counts a comment twice.
"""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from sqlalchemy import Select, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from collector.models import CommentFeatures, HitRoot, HitWord


@dataclass
class CountDelta:
    roots: Counter[str] = field(default_factory=Counter)
    words: Counter[str] = field(default_factory=Counter)
    word_roots: dict[str, str] = field(default_factory=dict)  # spelling -> root

    def change(
        self,
        old_words: Iterable[str] | None,
        old_roots: Iterable[str] | None,
        new: Mapping[str, str],
        old_weight: int = 1,
        new_weight: int = 1,
    ) -> None:
        """One comment going from its old words/roots, counted `old_weight` times, to `new`
        ({word: root}) counted `new_weight` times."""
        for word in new:
            self.words[word] += new_weight
        for word in set(old_words or ()):
            self.words[word] -= old_weight
        for root in set(new.values()):
            self.roots[root] += new_weight
        for root in set(old_roots or ()):
            self.roots[root] -= old_weight
        self.word_roots.update(new)


def apply_counts(session: Session, delta: CountDelta) -> None:
    """Add the delta to the stored counts; rows that drop to zero are removed."""
    roots = {root: n for root, n in delta.roots.items() if n}
    if roots:
        stmt = insert(HitRoot).values([{"root": r, "count": n} for r, n in roots.items()])
        grew = stmt.excluded.count > 0
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["root"],
                set_={
                    "count": HitRoot.count + stmt.excluded.count,
                    "last_seen_at": case((grew, func.now()), else_=HitRoot.last_seen_at),
                },
            )
        )
        session.execute(delete(HitRoot).where(HitRoot.root.in_(roots), HitRoot.count <= 0))

    words = {word: n for word, n in delta.words.items() if n}
    if words:
        rows = [
            {"word": w, "root": delta.word_roots.get(w, ""), "count": n} for w, n in words.items()
        ]
        stmt = insert(HitWord).values(rows)
        grew = stmt.excluded.count > 0
        session.execute(
            stmt.on_conflict_do_update(
                index_elements=["word"],
                set_={
                    "count": HitWord.count + stmt.excluded.count,
                    # A word seen again may now match another root (after lexicon edits).
                    "root": case((grew, stmt.excluded.root), else_=HitWord.root),
                    "last_seen_at": case((grew, func.now()), else_=HitWord.last_seen_at),
                },
            )
        )
        session.execute(delete(HitWord).where(HitWord.word.in_(words), HitWord.count <= 0))


def forget_comments(session: Session, comment_ids: Iterable[int] | Select[int]) -> None:
    """Take these comments' words and roots out of the counts (before deleting them).

    `comment_ids` is a list of ids or a select of them.
    """
    rows = session.execute(
        select(
            CommentFeatures.hit_words, CommentFeatures.hit_roots, CommentFeatures.hit_weight
        ).where(CommentFeatures.comment_id.in_(comment_ids))
    ).all()
    delta = CountDelta()
    for words, roots, weight in rows:
        delta.change(words, roots, {}, old_weight=weight)
    apply_counts(session, delta)
