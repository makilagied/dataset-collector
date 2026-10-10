"""Collection and processing statistics for `collector stats`."""

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collector.models import Account, Comment, CommentFeatures, Media, Sample
from collector.pipeline.sample import HIT_THRESHOLD


@dataclass
class AccountStats:
    account_id: int
    username: str
    category: str
    active: bool
    media: int
    comments: int
    processed: int
    eligible: int
    hits: int

    @property
    def hit_rate(self) -> float:
        return self.hits / self.eligible if self.eligible else 0.0


@dataclass
class BatchStats:
    name: str
    size: int
    exported: bool
    strata: dict[str, int]


@dataclass
class Stats:
    accounts: list[AccountStats] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)
    batches: list[BatchStats] = field(default_factory=list)

    @property
    def comments(self) -> int:
        return sum(a.comments for a in self.accounts)

    @property
    def processed(self) -> int:
        return sum(a.processed for a in self.accounts)

    @property
    def eligible(self) -> int:
        return sum(a.eligible for a in self.accounts)

    @property
    def hits(self) -> int:
        return sum(a.hits for a in self.accounts)

    @property
    def hit_rate(self) -> float:
        return self.hits / self.eligible if self.eligible else 0.0


def collect_stats(session: Session) -> Stats:
    stats = Stats()
    eligible = CommentFeatures.comment_id.is_not(None) & CommentFeatures.exclude_reason.is_(None)
    media_counts = (
        select(Media.account_id, func.count().label("n")).group_by(Media.account_id).subquery()
    )
    rows = session.execute(
        select(
            Account.id,
            Account.username,
            Account.category,
            Account.active,
            func.coalesce(media_counts.c.n, 0),
            func.count(Comment.id),
            func.count(CommentFeatures.comment_id),
            func.count(CommentFeatures.comment_id).filter(eligible),
            func.count(CommentFeatures.comment_id).filter(
                eligible & (CommentFeatures.match_score >= HIT_THRESHOLD)
            ),
        )
        .outerjoin(media_counts, media_counts.c.account_id == Account.id)
        .outerjoin(Media, Media.account_id == Account.id)
        .outerjoin(Comment, Comment.media_id == Media.id)
        .outerjoin(CommentFeatures, CommentFeatures.comment_id == Comment.id)
        .group_by(Account.id, media_counts.c.n)
        .order_by(Account.id)
    ).all()
    stats.accounts = [
        AccountStats(
            account_id=r[0],
            username=r[1] or "?",
            category=r[2] or "-",
            active=r[3],
            media=r[4],
            comments=r[5],
            processed=r[6],
            eligible=r[7],
            hits=r[8],
        )
        for r in rows
    ]

    lang = func.coalesce(CommentFeatures.lang, "unknown")
    stats.languages = dict(
        session.execute(
            select(lang, func.count()).where(eligible).group_by(lang).order_by(func.count().desc())
        )
        .tuples()
        .all()
    )
    stats.excluded = {
        str(reason): n
        for reason, n in session.execute(
            select(CommentFeatures.exclude_reason, func.count())
            .where(CommentFeatures.exclude_reason.is_not(None))
            .group_by(CommentFeatures.exclude_reason)
            .order_by(func.count().desc())
        ).tuples()
    }

    batches: dict[str, BatchStats] = {}
    for name, stratum, n, exported in session.execute(
        select(
            Sample.batch_name,
            Sample.stratum,
            func.count(),
            func.bool_and(Sample.exported_at.is_not(None)),
        )
        .group_by(Sample.batch_name, Sample.stratum)
        .order_by(Sample.batch_name)
    ).tuples():
        b = batches.setdefault(name, BatchStats(name, 0, True, {}))
        b.size += n
        b.strata[stratum] = n
        b.exported = b.exported and bool(exported)
    stats.batches = list(batches.values())
    return stats
