"""Background jobs for the admin UI.

Jobs are rows in the `jobs` table, run one at a time on a single worker thread so two
backfills never race. History survives restarts; jobs that were queued or running when the
process stopped are marked `interrupted` on startup.
"""

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
from sqlalchemy import CursorResult, select, update
from sqlalchemy.orm import Session, sessionmaker

from collector.accounts import refresh_expiring
from collector.config import get_settings
from collector.ig import oauth
from collector.ig.backfill import backfill_account
from collector.ig.client import GraphClient
from collector.logs import redact
from collector.models import Account, Job
from collector.pipeline.process import process
from collector.security import decrypt_token

log = logging.getLogger(__name__)

ACTIVE = ("queued", "running")


class JobError(Exception):
    """A job that can't run, with a message meant for the UI."""


def _account(session: Session, params: dict[str, Any]) -> Account:
    account = session.get(Account, int(params["account_id"]))
    if account is None:
        raise JobError(f"account {params['account_id']} not found")
    if account.ig_user_id.startswith("demo-"):
        raise JobError("demo account has no real token; use `collector demo load`")
    return account


def _backfill(session: Session, params: dict[str, Any]) -> str:
    account = _account(session, params)
    if not account.active:
        raise JobError(f"@{account.username} is paused; resume it first")
    since = date.fromisoformat(params["since"]) if params.get("since") else None
    settings = get_settings()
    with httpx.Client(timeout=30) as http:
        client = GraphClient(
            decrypt_token(account.access_token_enc), http=http, version=settings.graph_api_version
        )
        stats = backfill_account(session, client, account, since)
    return (
        f"@{account.username}: {stats.media_seen} posts, {stats.comments_seen} comments seen, "
        f"{stats.comments_inserted} new"
    )


def _process(session: Session, params: dict[str, Any]) -> str:
    stats = process(session, reprocess_all=bool(params.get("all")))
    excluded = ", ".join(f"{k} {v}" for k, v in stats.excluded.most_common()) or "none"
    return f"{stats.processed} processed, {stats.hits} hits, excluded: {excluded}"


def _refresh_tokens(session: Session, params: dict[str, Any]) -> str:
    days = int(params.get("days", 10))
    with httpx.Client(timeout=30) as http:
        results = refresh_expiring(session, http, timedelta(days=days))
    session.commit()
    if not results:
        return f"no tokens expire within {days} days"
    failed = [f"@{a.username}: {err}" for a, err in results if err]
    ok = len(results) - len(failed)
    if failed:
        raise JobError(f"{ok} refreshed, {len(failed)} failed: " + "; ".join(failed))
    return f"{ok} token(s) refreshed"


def _subscribe(session: Session, params: dict[str, Any]) -> str:
    account = _account(session, params)
    with httpx.Client(timeout=30) as http:
        oauth.subscribe_webhooks(http, get_settings(), decrypt_token(account.access_token_enc))
    return f"@{account.username}: subscribed to {', '.join(oauth.WEBHOOK_FIELDS)}"


HANDLERS: dict[str, Callable[[Session, dict[str, Any]], str]] = {
    "backfill": _backfill,
    "process": _process,
    "refresh_tokens": _refresh_tokens,
    "subscribe_webhooks": _subscribe,
}


def run_job(factory: sessionmaker[Session], job_id: int) -> None:
    with factory() as session:
        job = session.get(Job, job_id)
        if job is None or job.status != "queued":
            return
        job.status, job.started_at = "running", datetime.now(UTC)
        session.commit()
        try:
            job.result = HANDLERS[job.kind](session, dict(job.params or {}))
            job.status = "done"
        except Exception as e:
            session.rollback()
            job = session.get(Job, job_id)
            assert job is not None
            if not isinstance(e, JobError):
                log.exception("job %s (%s) failed", job_id, job.kind)
            message = str(e) if isinstance(e, JobError) else f"{type(e).__name__}: {e}"
            job.status, job.error = "failed", redact(message)[:2000]
        job.finished_at = datetime.now(UTC)
        session.commit()


class JobRunner:
    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job")

    def submit(self, factory: sessionmaker[Session], job_id: int) -> None:
        self._executor.submit(run_job, factory, job_id)


runner = JobRunner()


def enqueue(
    session: Session, factory: sessionmaker[Session], kind: str, params: dict[str, Any]
) -> tuple[Job, bool]:
    """Queue a job unless an identical one is already queued/running. Returns (job, created)."""
    if kind not in HANDLERS:
        raise JobError(f"unknown job kind {kind!r}")
    existing = session.scalars(
        select(Job).where(Job.kind == kind, Job.params == params, Job.status.in_(ACTIVE))
    ).first()
    if existing:
        return existing, False
    job = Job(kind=kind, params=params, status="queued")
    session.add(job)
    session.commit()
    runner.submit(factory, job.id)
    return job, True


def mark_interrupted(session: Session) -> int:
    result = session.execute(
        update(Job)
        .where(Job.status.in_(ACTIVE))
        .values(
            status="interrupted",
            finished_at=datetime.now(UTC),
            error="the app restarted before this job finished",
        )
    )
    session.commit()
    return cast(CursorResult[Any], result).rowcount or 0
