from collections.abc import Sequence

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from collector.ig.backfill import upsert_comment
from collector.models import Account, Comment, CommentFeatures, Media, Sample
from collector.nlp.langid import HeuristicDetector
from collector.pipeline.process import process
from collector.pipeline.sample import SampleError, parse_mix, quotas, sample_batch
from collector.security import encrypt_token

HITS = [f"wewe ni mjinga namba {i}" for i in range(10)]
CLEAN = [f"habari za leo namba {i}" for i in range(30)]
EXCLUDED = ["😂😂", "@juma @ali", "https://x.com"]


class KeywordScorer:
    """Test scorer: flags comments containing 'flag'."""

    name = "keyword"

    def score(self, texts: Sequence[str]) -> list[float]:
        return [0.9 if "flag" in t else 0.1 for t in texts]


@pytest.fixture
def pool(db: Session) -> Session:
    acct = Account(
        ig_user_id="1",
        username="a",
        access_token_enc=encrypt_token("t"),
        active=True,
        category="football",
    )
    db.add(acct)
    db.flush()
    media = Media(ig_media_id="M1", account_id=acct.id)
    db.add(media)
    db.flush()
    texts = HITS + CLEAN + EXCLUDED + ["flag hii ni mbaya", "flag tena sana"]
    for i, t in enumerate(texts):
        upsert_comment(db, media.id, {"id": f"c{i}", "text": t}, "backfill")
    db.commit()
    process(db, detector=HeuristicDetector())
    return db


def batch_ids(db: Session, batch: str) -> dict[int, str]:
    return dict(
        db.execute(select(Sample.comment_id, Sample.stratum).where(Sample.batch_name == batch))
        .tuples()
        .all()
    )


def texts_of(db: Session, ids: Sequence[int]) -> set[str]:
    return set(db.scalars(select(Comment.text_raw).where(Comment.id.in_(ids))))


# --- mix / quotas -----------------------------------------------------------------------


def test_parse_mix() -> None:
    assert parse_mix("50/30/20") == (50, 30, 20)
    assert parse_mix("100/0/0") == (100, 0, 0)


@pytest.mark.parametrize("bad", ["50/50", "a/b/c", "50/30/30", "120/-10/-10"])
def test_parse_mix_invalid(bad: str) -> None:
    with pytest.raises(SampleError):
        parse_mix(bad)


@pytest.mark.parametrize(
    ("size", "expected"),
    [(10, (5, 3, 2)), (7, (4, 2, 1)), (1, (1, 0, 0)), (3, (1, 1, 1))],
)
def test_quotas_sum_to_size(size: int, expected: tuple[int, int, int]) -> None:
    q = quotas(size, (50, 30, 20))
    assert tuple(q.values()) == expected
    assert sum(q.values()) == size


# --- sampling ---------------------------------------------------------------------------


def test_stub_scorer_moves_model_quota_to_random(pool: Session) -> None:
    result = sample_batch(pool, "b1", 10)
    assert result.requested == {"lexicon": 5, "model": 3, "random": 2}
    assert result.drawn == {"lexicon": 5, "model": 0, "random": 5}
    assert any("stub" in n for n in result.notes)
    chosen = batch_ids(pool, "b1")
    lexicon = [cid for cid, s in chosen.items() if s == "lexicon"]
    assert texts_of(pool, lexicon) <= set(HITS)


def test_model_stratum_uses_scorer(pool: Session) -> None:
    result = sample_batch(pool, "b1", 10, scorer=KeywordScorer())
    assert result.drawn["model"] == 2  # only two comments contain 'flag'
    model = [cid for cid, s in batch_ids(pool, "b1").items() if s == "model"]
    assert texts_of(pool, model) == {"flag hii ni mbaya", "flag tena sana"}


def test_excluded_never_sampled(pool: Session) -> None:
    sample_batch(pool, "all", 1000, (0, 0, 100))
    sampled = texts_of(pool, list(batch_ids(pool, "all")))
    assert not sampled & set(EXCLUDED)
    assert len(sampled) == len(HITS) + len(CLEAN) + 2


def test_no_comment_in_two_batches(pool: Session) -> None:
    sample_batch(pool, "b1", 20)
    sample_batch(pool, "b2", 20)
    assert not set(batch_ids(pool, "b1")) & set(batch_ids(pool, "b2"))
    total = pool.scalar(select(func.count()).select_from(Sample))
    distinct = pool.scalar(select(func.count(func.distinct(Sample.comment_id))))
    assert total == distinct


def test_db_enforces_one_batch_per_comment(pool: Session) -> None:
    cid = pool.scalars(select(Comment.id)).first()
    pool.add_all(
        [
            Sample(comment_id=cid, batch_name="x", stratum="random"),
            Sample(comment_id=cid, batch_name="y", stratum="random"),
        ]
    )
    with pytest.raises(IntegrityError):
        pool.commit()
    pool.rollback()


def test_pool_exhaustion_is_reported(pool: Session) -> None:
    result = sample_batch(pool, "big", 500)
    assert result.total == len(HITS) + len(CLEAN) + 2
    assert any("lexicon: only 10" in n for n in result.notes)
    assert any("random: only" in n for n in result.notes)
    empty = sample_batch(pool, "nothing-left", 5)
    assert empty.total == 0


def test_duplicate_batch_name_rejected(pool: Session) -> None:
    sample_batch(pool, "b1", 2)
    with pytest.raises(SampleError, match="already exists"):
        sample_batch(pool, "b1", 2)


def test_invalid_size(pool: Session) -> None:
    with pytest.raises(SampleError):
        sample_batch(pool, "b0", 0)


def test_seed_is_reproducible(pool: Session) -> None:
    sample_batch(pool, "s1", 8, seed=42)
    first = batch_ids(pool, "s1")
    pool.execute(delete(Sample))
    pool.commit()
    sample_batch(pool, "s1", 8, seed=42)
    assert batch_ids(pool, "s1") == first


def test_lang_filter(pool: Session) -> None:
    pool.execute(
        CommentFeatures.__table__.update()
        .where(CommentFeatures.text_norm.like("habari%"))
        .values(lang="en")
    )
    pool.commit()
    sample_batch(pool, "en-only", 100, (0, 0, 100), lang="en")
    assert texts_of(pool, list(batch_ids(pool, "en-only"))) == set(CLEAN)


def test_sample_ids_do_not_group_by_stratum(pool: Session) -> None:
    sample_batch(pool, "b1", 20, seed=1)
    strata = pool.scalars(
        select(Sample.stratum).where(Sample.batch_name == "b1").order_by(Sample.id)
    ).all()
    assert strata != sorted(strata)
