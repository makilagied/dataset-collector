"""Stratified sampling of processed comments into labelling batches.

Strata:
  lexicon - lexicon hits (match_score >= HIT_THRESHOLD)
  model   - comments the scorer flags that the lexicon did not
  random  - uniform over the remaining eligible pool, to estimate base rates

Eligible: processed, not excluded (empty/emoji/link/mention-only/duplicate) and not in any
earlier batch. A comment can be in at most one batch (samples.comment_id is unique).
A stratum short of candidates passes its shortfall to the next one, ending at random.
"""

import random
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select, exists, func, or_, select
from sqlalchemy.orm import Session

from collector.models import Comment, CommentFeatures, Sample
from collector.pipeline.scoring import Scorer, StubScorer

HIT_THRESHOLD = 0.5  # cued or non-context-dependent lexicon match
MODEL_THRESHOLD = 0.5
CANDIDATE_FACTOR = 20  # comments scored per model slot
STRATA = ("lexicon", "model", "random")


class SampleError(ValueError):
    pass


def parse_mix(mix: str) -> tuple[int, int, int]:
    try:
        parts = tuple(int(p) for p in mix.split("/"))
    except ValueError as e:
        raise SampleError(f"mix must look like 50/30/20, got {mix!r}") from e
    if len(parts) != 3 or any(p < 0 for p in parts) or sum(parts) != 100:
        raise SampleError(f"mix needs three non-negative parts summing to 100, got {mix!r}")
    return parts[0], parts[1], parts[2]


def quotas(size: int, mix: tuple[int, int, int]) -> dict[str, int]:
    """Split `size` by percentages using largest remainders, so the parts sum to size."""
    exact = [size * p / 100 for p in mix]
    counts = [int(x) for x in exact]
    by_remainder = sorted(range(3), key=lambda i: exact[i] - counts[i], reverse=True)
    for i in by_remainder[: size - sum(counts)]:
        counts[i] += 1
    return dict(zip(STRATA, counts, strict=True))


@dataclass
class SampleResult:
    batch: str
    requested: dict[str, int]
    drawn: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(self.drawn.values())


def _eligible(lang: str | None) -> Select[Any]:
    query = (
        select(Comment.id)
        .join(CommentFeatures, CommentFeatures.comment_id == Comment.id)
        .where(
            CommentFeatures.exclude_reason.is_(None),
            ~exists().where(Sample.comment_id == Comment.id),
        )
    )
    if lang:
        query = query.where(CommentFeatures.lang == lang)
    return query


def _seed_to_pg(seed: int) -> float:
    """Postgres setseed() takes a float in [-1, 1]."""
    return ((seed % 2001) - 1000) / 1000


def sample_batch(
    session: Session,
    batch: str,
    size: int,
    mix: tuple[int, int, int] = (50, 30, 20),
    *,
    scorer: Scorer | None = None,
    seed: int | None = None,
    lang: str | None = None,
) -> SampleResult:
    if size <= 0:
        raise SampleError("size must be positive")
    if session.scalar(select(exists().where(Sample.batch_name == batch))):
        raise SampleError(f"batch {batch!r} already exists")
    scorer = scorer or StubScorer()
    if seed is not None:
        session.execute(select(func.setseed(_seed_to_pg(seed))))

    result = SampleResult(batch=batch, requested=quotas(size, mix))
    chosen: dict[int, str] = {}

    def not_chosen(query: Select[Any]) -> Select[Any]:
        return query.where(Comment.id.not_in(chosen)) if chosen else query

    # 1. lexicon hits
    want = result.requested["lexicon"]
    lexicon_ids = session.scalars(
        _eligible(lang)
        .where(CommentFeatures.match_score >= HIT_THRESHOLD)
        .order_by(func.random())
        .limit(want)
    ).all()
    chosen.update(dict.fromkeys(lexicon_ids, "lexicon"))
    carry = want - len(lexicon_ids)
    if carry:
        result.notes.append(f"lexicon: only {len(lexicon_ids)} of {want} available")

    # 2. model-flagged among non-hits: score a random candidate set, keep the top
    want = result.requested["model"] + carry
    model_ids: list[int] = []
    if want:
        candidates = session.execute(
            not_chosen(_eligible(lang))
            .add_columns(Comment.text_raw)
            .where(
                or_(
                    CommentFeatures.match_score < HIT_THRESHOLD,
                    CommentFeatures.match_score.is_(None),
                )
            )
            .order_by(func.random())
            .limit(want * CANDIDATE_FACTOR)
        ).all()
        scores = scorer.score([text for _, text in candidates]) if candidates else []
        flagged = sorted(
            (
                (s, cid)
                for (cid, _), s in zip(candidates, scores, strict=True)
                if s >= MODEL_THRESHOLD
            ),
            reverse=True,
        )
        model_ids = [cid for _, cid in flagged[:want]]
        chosen.update(dict.fromkeys(model_ids, "model"))
        if len(model_ids) < want:
            result.notes.append(
                f"model: {len(model_ids)} of {want} flagged by scorer {scorer.name!r}; "
                "remainder moved to random"
            )
    carry = want - len(model_ids)

    # 3. random over everything still eligible
    want = result.requested["random"] + carry
    random_ids = (
        session.scalars(not_chosen(_eligible(lang)).order_by(func.random()).limit(want)).all()
        if want
        else []
    )
    chosen.update(dict.fromkeys(random_ids, "random"))
    if len(random_ids) < want:
        result.notes.append(f"random: only {len(random_ids)} of {want} eligible comments left")

    # Insert in shuffled order so sample ids don't reveal the stratum to annotators.
    order = list(chosen.items())
    random.Random(seed).shuffle(order)
    session.add_all(Sample(comment_id=cid, batch_name=batch, stratum=s) for cid, s in order)
    session.commit()

    result.drawn = {s: sum(1 for v in chosen.values() if v == s) for s in STRATA}
    return result
