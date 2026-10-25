"""Run scrapes and browser logins. Shared by the CLI and the admin UI's background jobs."""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from collector.config import get_settings
from collector.ig.backfill import _get_state, _set_state
from collector.scrape.browser import (
    LOGIN_URLS,
    TikTokSite,
    first_page,
    instagram_fetch,
    instagram_post_lister,
    logged_in,
    open_browser,
)
from collector.scrape.common import (
    LOGIN_REQUIRED,
    PLATFORMS,
    BlockedError,
    Pacer,
    ScrapeError,
    ScrapeStats,
)

LOGIN_TIMEOUT = 300.0  # seconds a login window waits for you
log = logging.getLogger(__name__)


@dataclass
class ScrapeOptions:
    max_posts: int = 20
    max_comments: int = 5000
    since: date | None = None  # Instagram and TikTok
    category: str | None = None
    delay: float = 4.0
    refresh: bool = False
    only_hits: bool | None = None  # None: follow STORE_ONLY_HITS; False: store everything


@dataclass
class TargetResult:
    target: str
    stats: ScrapeStats | None = None
    error: str | None = None

    def __str__(self) -> str:
        return f"{self.target}: {self.stats}" if self.stats else f"{self.target}: {self.error}"


@dataclass
class ScrapeRun:
    results: list[TargetResult] = field(default_factory=list)
    blocked: str | None = None  # why the run stopped early, if it did

    @property
    def failed(self) -> int:
        return sum(r.stats is None for r in self.results)


def unavailable_reason() -> str | None:
    """Why this process can't drive a browser, or None if it can."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "Playwright is not installed in this environment"
    return None


def profile_dir() -> Path:
    return Path(get_settings().scrape_profile_dir)


def _login_key(platform: str) -> str:
    return f"scrape:login:{platform}"


def record_login(session: Session, platform: str, ok: bool) -> None:
    _set_state(session, _login_key(platform), {"ok": ok, "at": datetime.now(UTC).isoformat()})
    session.commit()


def login_status(session: Session) -> dict[str, dict[str, Any] | None]:
    """Last known login state per platform, as seen by the most recent login or scrape."""
    return {p: _get_state(session, _login_key(p)) for p in PLATFORMS}


def wait_for_login(session: Session, platform: str, timeout: float = LOGIN_TIMEOUT) -> bool:
    """Open the login page and wait (up to `timeout`) until the session cookie appears."""
    ok = False
    with open_browser(profile_dir()) as context:
        page = first_page(context)
        if not logged_in(context, platform):
            page.goto(LOGIN_URLS[platform])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if logged_in(context, platform):
                ok = True
                page.wait_for_timeout(3000)  # let the site finish setting its cookies
                break
            try:
                page.wait_for_timeout(2000)
            except Exception:  # the window was closed by hand
                break
    record_login(session, platform, ok)
    return ok


def run_scrape(
    session: Session,
    platform: str,
    targets: list[str],
    options: ScrapeOptions,
    progress: Callable[[str], None] | None = None,
) -> ScrapeRun:
    from collector.scrape.facebook import FacebookScraper
    from collector.scrape.instagram import InstagramScraper
    from collector.scrape.tiktok import TikTokScraper

    if platform not in PLATFORMS:
        raise ScrapeError(f"platform must be one of {', '.join(PLATFORMS)}")
    run = ScrapeRun()
    pacer = Pacer(options.delay, max_per_hour=get_settings().scrape_max_requests_per_hour)
    with open_browser(profile_dir()) as context:
        ok = logged_in(context, platform)
        record_login(session, platform, ok)
        if not ok and platform in LOGIN_REQUIRED:
            run.blocked = f"not logged in to {platform}; log in first"
            return run
        page = first_page(context)
        scraper: InstagramScraper | FacebookScraper | TikTokScraper
        if platform == "tiktok":
            scraper = TikTokScraper(
                session,
                TikTokSite(page, pacer),
                pacer,
                max_posts=options.max_posts,
                max_comments=options.max_comments,
                since=options.since,
                category=options.category,
                refresh=options.refresh,
                only_hits=options.only_hits,
                progress=progress,
            )
        elif platform == "instagram":
            scraper = InstagramScraper(
                session,
                instagram_fetch(page),
                instagram_post_lister(page, pacer),
                pacer,
                max_posts=options.max_posts,
                max_comments=options.max_comments,
                since=options.since,
                category=options.category,
                refresh=options.refresh,
                only_hits=options.only_hits,
                progress=progress,
            )
        else:
            scraper = FacebookScraper(
                session,
                page,
                pacer,
                max_posts=options.max_posts,
                max_comments=options.max_comments,
                category=options.category,
                refresh=options.refresh,
                only_hits=options.only_hits,
                progress=progress,
            )
        for target in targets:
            try:
                run.results.append(TargetResult(target, stats=scraper.scrape(target)))
            except BlockedError as e:
                session.rollback()
                run.results.append(TargetResult(target, error="stopped"))
                run.blocked = str(e)
                break
            except ScrapeError as e:
                session.rollback()
                run.results.append(TargetResult(target, error=f"skipped: {e}"))
            except Exception as e:  # a browser error must not lose the other targets
                log.exception("scraping %s failed", target)
                session.rollback()
                first = str(e).splitlines()[0] if str(e) else type(e).__name__
                run.results.append(TargetResult(target, error=f"skipped: {first}"))
    return run
