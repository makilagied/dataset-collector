"""Admin UI: accounts, scraping, comments, batches and background jobs, at /admin.

Security:
- Login page with a signed session cookie (HttpOnly, SameSite=Lax, 12 h). Credentials are
  ADMIN_USER / ADMIN_PASSWORD; the UI is disabled while the password is empty. HTTP Basic
  auth is also accepted, for scripts. Changing the password invalidates all sessions.
- Refused through proxies/tunnels (ngrok) unless ADMIN_ALLOW_REMOTE.
- A CSRF token on every form that changes state.
- Jinja2 autoescaping for all (attacker-written) comment text.
"""

import asyncio
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BeforeValidator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from collector.admin import jobs
from collector.config import Settings, get_settings
from collector.db import get_sessionmaker
from collector.models import Account, Comment, CommentFeatures, HitRoot, HitWord, Job, Media
from collector.nlp.highlight import segments
from collector.pipeline.export import (
    FORMATS,
    SHOW_FILTERS,
    ExportError,
    file_encoding,
    filter_comments,
    iter_all_comments,
    render_batch,
)
from collector.pipeline.sample import HIT_THRESHOLD, SampleError, parse_mix, sample_batch
from collector.pipeline.scoring import SCORERS
from collector.pipeline.stats import collect_stats
from collector.scrape.common import PLATFORM_LABELS, PLATFORM_NAMES, PLATFORMS

PAGE_SIZE = 50
# An optional id filter: links and forms send "account=" for "all", which means None.
OptionalId = Annotated[int | None, BeforeValidator(lambda v: v or None), Query()]
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["dt"] = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else "—"
_basic = HTTPBasic(auto_error=False)


# --- security -----------------------------------------------------------------------


SESSION_COOKIE = "collector_admin"
SESSION_SECONDS = 12 * 3600


def _key(settings: Settings, purpose: bytes) -> str:
    return hmac.new((settings.admin_password or "").encode(), purpose, hashlib.sha256).hexdigest()


def csrf_token(settings: Settings) -> str:
    return _key(settings, b"collector-admin-csrf")


def make_session(settings: Settings, now: float | None = None) -> str:
    expires = int(now if now is not None else time.time()) + SESSION_SECONDS
    sig = hmac.new(_key(settings, b"session").encode(), str(expires).encode(), hashlib.sha256)
    return f"{expires}.{sig.hexdigest()}"


def valid_session(settings: Settings, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    expires, sig = token.split(".", 1)
    if not expires.isdigit():
        return False
    expected = make_session(settings, int(expires) - SESSION_SECONDS).split(".", 1)[1]
    current = now if now is not None else time.time()
    return hmac.compare_digest(sig, expected) and int(expires) > current


def credentials_ok(settings: Settings, username: str, password: str) -> bool:
    return bool(settings.admin_password) and (
        secrets.compare_digest(username.encode(), settings.admin_user.encode())
        & secrets.compare_digest(password.encode(), (settings.admin_password or "").encode())
    )


def safe_next(target: str | None, default: str = "/admin") -> str:
    """Only same-site /admin paths: no open redirects."""
    if not target or not target.startswith("/admin") or "//" in target or "\\" in target:
        return default
    return target


def require_local(request: Request, settings: Annotated[Settings, Depends(get_settings)]) -> None:
    if not settings.admin_password:
        raise HTTPException(503, "Admin UI is disabled: set ADMIN_PASSWORD in .env")
    if not settings.admin_allow_remote and (
        "x-forwarded-for" in request.headers or "forwarded" in request.headers
    ):
        raise HTTPException(403, "Admin UI is only available locally (ADMIN_ALLOW_REMOTE=false)")


def require_admin(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    credentials: Annotated[HTTPBasicCredentials | None, Depends(_basic)],
) -> None:
    if valid_session(settings, request.cookies.get(SESSION_COOKIE)):
        return
    if credentials and credentials_ok(settings, credentials.username, credentials.password):
        return
    if request.method == "GET":
        here = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise HTTPException(303, headers={"Location": "/admin/login?" + urlencode({"next": here})})
    raise HTTPException(401, "Authentication required: log in at /admin/login")


async def _parse_form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}


async def read_form(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, str]:
    data = await _parse_form(request)
    if not hmac.compare_digest(data.get("csrf", ""), csrf_token(settings)):
        raise HTTPException(403, "Invalid form token; reload the page and try again")
    return data


Factory = Annotated[sessionmaker[Session], Depends(get_sessionmaker)]


def get_db(factory: Factory) -> Iterator[Session]:
    with factory() as session:
        yield session


Db = Annotated[Session, Depends(get_db)]
Form = Annotated[dict[str, str], Depends(read_form)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# Login/logout pages: enabled + local checks only. Everything else also needs a session.
public = APIRouter(prefix="/admin", dependencies=[Depends(require_local)])
router = APIRouter(prefix="/admin", dependencies=[Depends(require_local), Depends(require_admin)])


def render(
    request: Request, name: str, settings: Settings, status: int = 200, **ctx: Any
) -> HTMLResponse:
    ctx.update(
        csrf=csrf_token(settings),
        flash=request.query_params.get("msg"),
        flash_kind=request.query_params.get("kind", "ok"),
        path=request.url.path,
        user=settings.admin_user,
    )
    return templates.TemplateResponse(request, name, ctx, status_code=status)


def back(form: dict[str, str], msg: str, kind: str = "ok", default: str = "/admin") -> Response:
    target = safe_next(form.get("next"), default)
    sep = "&" if "?" in target else "?"
    return RedirectResponse(f"{target}{sep}{urlencode({'msg': msg, 'kind': kind})}", 303)


def _queue(
    db: Session,
    factory: sessionmaker[Session],
    form: dict[str, str],
    kind: str,
    params: dict[str, Any],
    label: str,
) -> Response:
    job, created = jobs.enqueue(db, factory, kind, params)
    if created:
        return back(form, f"{label} started (job #{job.id}). Progress is on the Jobs page.")
    return back(form, f"{label} is already {job.status} (job #{job.id}).", "warn")


# --- login ---------------------------------------------------------------------------


@public.get("/login", response_class=HTMLResponse)
def login_page(request: Request, settings: SettingsDep, next: str = "/admin") -> Response:
    if valid_session(settings, request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(safe_next(next), 303)
    return render(request, "login.html", settings, next=safe_next(next), error=None)


@public.post("/login", response_class=HTMLResponse)
async def login(request: Request, settings: SettingsDep) -> Response:
    form = await _parse_form(request)
    target = safe_next(form.get("next"))
    if not credentials_ok(settings, form.get("username", ""), form.get("password", "")):
        await asyncio.sleep(1)  # slow down password guessing
        return render(
            request,
            "login.html",
            settings,
            status=401,
            next=target,
            error="Wrong username or password.",
        )
    response = RedirectResponse(target, 303)
    response.set_cookie(
        SESSION_COOKIE,
        make_session(settings),
        max_age=SESSION_SECONDS,
        path="/admin",
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https",
    )
    return response


@router.post("/logout")
def logout(form: Form) -> Response:
    response = RedirectResponse("/admin/login", 303)
    response.delete_cookie(SESSION_COOKIE, path="/admin")
    return response


# --- overview ------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Db, settings: SettingsDep) -> HTMLResponse:
    stats = collect_stats(db)
    recent = db.scalars(select(Job).order_by(Job.id.desc()).limit(6)).all()
    top_words = db.scalars(
        select(HitRoot).order_by(HitRoot.count.desc(), HitRoot.root).limit(15)
    ).all()
    spellings: dict[str, list[str]] = {r.root: [] for r in top_words}
    for w in db.scalars(
        select(HitWord).where(HitWord.root.in_(spellings)).order_by(HitWord.count.desc())
    ):
        spellings[w.root].append(f"{w.word} {w.count}")
    return render(
        request,
        "dashboard.html",
        settings,
        stats=stats,
        recent_jobs=recent,
        top_words=top_words,
        top_max=max((r.count for r in top_words), default=0),
        spellings=spellings,
        unprocessed=stats.comments - stats.processed,
    )


@router.post("/process")
def run_process(db: Db, factory: Factory, form: Form) -> Response:
    everything = form.get("all") == "1"
    return _queue(
        db,
        factory,
        form,
        "process",
        {"all": everything},
        "Reprocessing all comments" if everything else "Processing new comments",
    )


@router.post("/tokens/refresh")
def refresh_tokens(db: Db, factory: Factory, form: Form) -> Response:
    return _queue(db, factory, form, "refresh_tokens", {"days": 10}, "Token refresh")


# --- accounts ------------------------------------------------------------------------


def _public_base(settings: Settings) -> str:
    return re.sub(r"/oauth/callback/?$", "", settings.ig_redirect_uri)


@router.get("/accounts", response_class=HTMLResponse)
def accounts(
    request: Request, db: Db, settings: SettingsDep, consent_ref: str | None = None
) -> HTMLResponse:
    counts = {a.account_id: a for a in collect_stats(db).accounts}
    last_sync = dict(
        db.execute(
            select(Media.account_id, func.max(Media.last_synced_at)).group_by(Media.account_id)
        )
        .tuples()
        .all()
    )
    rows = [
        {
            "account": a,
            "stats": counts.get(a.id),
            "last_sync": last_sync.get(a.id),
            "demo": a.ig_user_id.startswith("demo-"),
            "scraped": a.ig_user_id.startswith("scrape:"),
        }
        for a in db.scalars(select(Account).order_by(Account.id))
    ]
    base = _public_base(settings)
    link = f"{base}/connect?{urlencode({'consent_ref': consent_ref})}" if consent_ref else None
    return render(
        request,
        "accounts.html",
        settings,
        rows=rows,
        connect_base=base,
        connect_link=link,
        consent_ref=consent_ref or "",
        now=datetime.now().astimezone(),
    )


def _get_account(db: Session, account_id: int) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(404, "account not found")
    return account


@router.post("/accounts/{account_id}/backfill")
def account_backfill(account_id: int, db: Db, factory: Factory, form: Form) -> Response:
    account = _get_account(db, account_id)
    params: dict[str, Any] = {"account_id": account_id}
    since = form.get("since", "").strip()
    if since:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            return back(form, "Since must be a date like 2026-01-31", "error")
        params["since"] = since
    return _queue(db, factory, form, "backfill", params, f"Backfill of @{account.username}")


@router.post("/accounts/{account_id}/subscribe")
def account_subscribe(account_id: int, db: Db, factory: Factory, form: Form) -> Response:
    account = _get_account(db, account_id)
    return _queue(
        db,
        factory,
        form,
        "subscribe_webhooks",
        {"account_id": account_id},
        f"Webhook subscription for @{account.username}",
    )


@router.post("/accounts/{account_id}/category")
def account_category(account_id: int, db: Db, form: Form) -> Response:
    account = _get_account(db, account_id)
    category = form.get("category", "").strip().lower()[:40] or None
    account.category = category
    db.commit()
    return back(form, f"@{account.username}: category set to {category or '(none)'}")


@router.post("/accounts/{account_id}/active")
def account_active(account_id: int, db: Db, form: Form) -> Response:
    account = _get_account(db, account_id)
    account.active = form.get("active") == "1"
    db.commit()
    state = "resumed" if account.active else "paused (no backfills, webhooks ignored)"
    return back(form, f"@{account.username} {state}")


# --- scraping ------------------------------------------------------------------------

MAX_TARGETS = 50
SCRAPE_LIMITS = {"max_posts": (1, 500), "max_comments": (1, 100_000), "delay": (1, 60)}


def _split(text: str) -> list[str]:
    return list(dict.fromkeys(t for t in re.split(r"[\s,]+", text) if t))


@router.get("/scrape", response_class=HTMLResponse)
def scrape_page(request: Request, db: Db, settings: SettingsDep) -> HTMLResponse:
    from collector.scrape.run import login_status, unavailable_reason

    counts = {a.account_id: a for a in collect_stats(db).accounts}
    sources = [
        {
            "account": a,
            "stats": counts.get(a.id),
            "platform": PLATFORM_NAMES.get(a.ig_user_id.split(":")[1], "?"),
        }
        for a in db.scalars(
            select(Account).where(Account.ig_user_id.startswith("scrape:")).order_by(Account.id)
        )
    ]
    recent = db.scalars(
        select(Job).where(Job.kind.in_(["scrape", "scrape_login"])).order_by(Job.id.desc()).limit(8)
    ).all()
    return render(
        request,
        "scrape.html",
        settings,
        unavailable=unavailable_reason(),
        logins=login_status(db),
        sources=sources,
        recent_jobs=recent,
        active=any(j.status in jobs.ACTIVE for j in recent),
        store_only_hits=settings.store_only_hits,
    )


@router.post("/scrape/login")
def scrape_login(db: Db, factory: Factory, form: Form) -> Response:
    from collector.scrape.run import unavailable_reason

    platform = form.get("platform", "")
    if platform not in PLATFORMS:
        return back(form, "Unknown platform", "error", "/admin/scrape")
    if reason := unavailable_reason():
        return back(form, f"Can't open a browser here: {reason}", "error", "/admin/scrape")
    job, created = jobs.enqueue(db, factory, "scrape_login", {"platform": platform})
    if not created:
        return back(form, f"Login is already {job.status} (job #{job.id}).", "warn")
    return back(
        form,
        f"A browser window will open on this computer (job #{job.id}): log in to "
        f"{PLATFORM_LABELS[platform]} there. It closes by itself once you're in.",
    )


@router.post("/scrape")
def scrape_start(db: Db, factory: Factory, form: Form) -> Response:
    from collector.scrape import facebook, instagram, tiktok
    from collector.scrape.common import ScrapeError
    from collector.scrape.run import unavailable_reason

    def error(msg: str) -> Response:
        return back(form, msg, "error", "/admin/scrape")

    platform = form.get("platform", "")
    if platform not in PLATFORMS:
        return error("Unknown platform")
    if reason := unavailable_reason():
        return error(f"Can't open a browser here: {reason}")
    targets = _split(form.get("targets", ""))
    if not targets:
        return error("Add at least one target")
    if len(targets) > MAX_TARGETS:
        return error(f"At most {MAX_TARGETS} targets per run")
    parse = {
        "instagram": instagram.parse_target,
        "facebook": facebook.parse_target,
        "tiktok": tiktok.parse_target,
    }[platform]
    bad = []
    for t in targets:
        try:
            parse(t)
        except ScrapeError:
            bad.append(t)
    if bad:
        return error(f"Not a valid {PLATFORM_LABELS[platform]} target: {', '.join(bad)}")

    params: dict[str, Any] = {"platform": platform, "targets": targets}
    try:
        for name, (lo, hi) in SCRAPE_LIMITS.items():
            value = float(form.get(name) or 0)
            if not lo <= value <= hi:
                return error(f"{name.replace('_', ' ')} must be between {lo} and {hi}")
            params[name] = value if name == "delay" else int(value)
    except ValueError:
        return error("Numbers only in the limits")
    since = form.get("since", "").strip()
    if since and platform in ("instagram", "tiktok"):
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            return error("Since must be a date like 2026-01-31")
        params["since"] = since
    category = form.get("category", "").strip().lower()[:40]
    if category:
        params["category"] = category
    for flag in ("refresh", "process"):
        if form.get(flag) == "1":
            params[flag] = True
    store = form.get("store", "")
    if store not in ("", "hits", "all"):
        return error("Store must be hits or all")
    if store:
        params["store"] = store
    label = f"{PLATFORM_LABELS[platform]} scrape of {len(targets)} target(s)"
    return _queue(db, factory, form, "scrape", params, label)


# --- posts ---------------------------------------------------------------------------


@router.get("/posts", response_class=HTMLResponse)
def posts(
    request: Request,
    db: Db,
    settings: SettingsDep,
    account: OptionalId = None,
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """Posts with Instagram's reported comment count next to what was actually collected."""
    eligible_hit = (CommentFeatures.match_score >= HIT_THRESHOLD) & (
        CommentFeatures.exclude_reason.is_(None)
    )
    query = (
        select(
            Media,
            Account.username,
            func.count(Comment.id).label("collected"),
            func.count(CommentFeatures.comment_id).filter(eligible_hit).label("hits"),
            func.count(Comment.id)
            .filter(CommentFeatures.comment_id.is_(None))
            .label("unprocessed"),
        )
        .join(Account, Account.id == Media.account_id)
        .outerjoin(Comment, Comment.media_id == Media.id)
        .outerjoin(CommentFeatures, CommentFeatures.comment_id == Comment.id)
        .group_by(Media.id, Account.username)
    )
    per_post = (
        select(Media.comments_count, func.count(Comment.id).label("n"))
        .outerjoin(Comment, Comment.media_id == Media.id)
        .group_by(Media.id)
    )
    if account:
        query = query.where(Media.account_id == account)
        per_post = per_post.where(Media.account_id == account)
    pp = per_post.subquery()
    reported_col = func.coalesce(pp.c.comments_count, 0)
    n_posts, reported, collected, missing = db.execute(
        select(
            func.count(),
            func.coalesce(func.sum(reported_col), 0),
            func.coalesce(func.sum(pp.c.n), 0),
            # per post, so extra replies on one post can't hide a gap on another
            func.coalesce(func.sum(func.greatest(reported_col - pp.c.n, 0)), 0),
        )
    ).one()
    rows = db.execute(
        query.order_by(Media.posted_at.desc().nulls_last(), Media.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    accounts_list = db.scalars(select(Account).order_by(Account.username)).all()
    return render(
        request,
        "posts.html",
        settings,
        rows=rows,
        n_posts=n_posts,
        reported=reported,
        collected=collected,
        missing=missing,
        page=page,
        pages=max(1, -(-n_posts // PAGE_SIZE)),
        accounts=accounts_list,
        account=account or "",
        page_url=lambda p: "/admin/posts?" + urlencode({"account": account or "", "page": p}),
    )


# --- comments ------------------------------------------------------------------------


@router.get("/comments", response_class=HTMLResponse)
def comments(
    request: Request,
    db: Db,
    settings: SettingsDep,
    account: OptionalId = None,
    show: str = "all",
    q: str = "",
    media: OptionalId = None,
    root: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    if show not in SHOW_FILTERS:
        show = "all"
    query = filter_comments(
        select(Comment, CommentFeatures, Media.permalink, Account.username)
        .join(Media, Media.id == Comment.media_id)
        .join(Account, Account.id == Media.account_id)
        .outerjoin(CommentFeatures, CommentFeatures.comment_id == Comment.id),
        account,
        show,
        q,
        media,
        root,
    )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(
        query.order_by(Comment.commented_at.desc().nulls_last(), Comment.id.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    accounts_list = db.scalars(select(Account).order_by(Account.username)).all()
    filters = {"account": account or "", "show": show, "q": q, "media": media or "", "root": root}
    return render(
        request,
        "comments.html",
        settings,
        post=db.get(Media, media) if media else None,
        rows=rows,
        marked={c.id: segments(c.text_raw or "") for c, *_ in rows},
        total=total,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        accounts=accounts_list,
        filters=filters,
        hit_threshold=HIT_THRESHOLD,
        page_url=lambda p: "/admin/comments?" + urlencode({**filters, "page": p}),
        export_url=lambda fmt: "/admin/comments/export?" + urlencode({**filters, "fmt": fmt}),
    )


@router.get("/comments/export")
def export_comments(
    factory: Factory,
    fmt: str = "csv",
    account: OptionalId = None,
    show: str = "all",
    q: str = "",
    media: OptionalId = None,
    root: str = "",
) -> Response:
    """Download every comment matching the filters (abusive or not), anonymized."""
    if fmt not in FORMATS or show not in SHOW_FILTERS:
        raise HTTPException(400, f"fmt must be one of {FORMATS}, show one of {SHOW_FILTERS}")

    def stream() -> Iterator[bytes]:
        # Own session: the request's dependency session is closed before streaming starts.
        with factory() as session:
            for piece in iter_all_comments(
                session, fmt, account_id=account, show=show, q=q, media_id=media, root=root
            ):
                yield piece.encode("utf-8")

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    name = f"comments-{show}-{stamp}.{fmt}"
    mime = "text/csv" if fmt == "csv" else "application/x-ndjson"
    return StreamingResponse(
        stream(),
        media_type=f"{mime}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# --- batches -------------------------------------------------------------------------


@router.get("/batches", response_class=HTMLResponse)
def batches(request: Request, db: Db, settings: SettingsDep) -> HTMLResponse:
    stats = collect_stats(db)
    return render(
        request,
        "batches.html",
        settings,
        batches=stats.batches,
        eligible=stats.eligible,
        scorers=list(SCORERS),
    )


@router.post("/batches")
def create_batch(db: Db, form: Form) -> Response:
    name = form.get("batch", "").strip()
    if not re.fullmatch(r"[\w.-]{1,60}", name):
        return back(form, "Batch name: 1-60 letters, digits, '-', '_' or '.'", "error")
    try:
        size = int(form.get("size", "0"))
        seed = int(form["seed"]) if form.get("seed", "").strip() else None
        scorer = SCORERS[form.get("scorer", "stub")]()
        result = sample_batch(
            db,
            name,
            size,
            parse_mix(form.get("mix", "50/30/20")),
            scorer=scorer,
            seed=seed,
            lang=form.get("lang") or None,
        )
    except (ValueError, KeyError, SampleError) as e:
        db.rollback()
        return back(form, f"Could not create batch: {e}", "error")
    parts = ", ".join(f"{s} {n}" for s, n in result.drawn.items())
    notes = (" — " + "; ".join(result.notes)) if result.notes else ""
    return back(
        form,
        f"Batch {name!r}: {result.total} comments ({parts}){notes}",
        "ok" if result.total else "warn",
    )


@router.get("/batches/{name}/export")
def download_batch(name: str, db: Db, fmt: str = "csv") -> Response:
    try:
        text, _ = render_batch(db, name, fmt)
    except ExportError as e:
        raise HTTPException(404, str(e)) from e
    safe = re.sub(r"[^\w.-]", "_", name)
    media = "text/csv" if fmt == "csv" else "application/x-ndjson"
    return Response(
        text.encode(file_encoding(fmt)),
        media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{safe}.{fmt}"'},
    )


# --- hit words -----------------------------------------------------------------------


@router.get("/words", response_class=HTMLResponse)
def hit_word_list(
    request: Request,
    db: Db,
    settings: SettingsDep,
    q: str = "",
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    """One row per root ("jinga"), with the spellings seen ("mjinga", "wajinga", ...)."""
    query = select(HitRoot)
    if q:
        needle = q.lower()
        spelled = select(HitWord.root).where(HitWord.word.contains(needle, autoescape=True))
        query = query.where(
            HitRoot.root.contains(needle, autoescape=True) | HitRoot.root.in_(spelled)
        )
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.scalars(
        query.order_by(HitRoot.count.desc(), HitRoot.root)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()
    forms: dict[str, list[HitWord]] = {r.root: [] for r in rows}
    for w in db.scalars(
        select(HitWord).where(HitWord.root.in_(forms)).order_by(HitWord.count.desc(), HitWord.word)
    ):
        forms[w.root].append(w)

    def page_url(n: int) -> str:
        return "/admin/words?" + urlencode({"q": q, "page": n})

    return render(
        request,
        "words.html",
        settings,
        rows=rows,
        forms=forms,
        total=total,
        q=q,
        page=page,
        pages=max(1, -(-total // PAGE_SIZE)),
        page_url=page_url,
    )


# --- jobs ----------------------------------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse)
def job_list(request: Request, db: Db, settings: SettingsDep) -> HTMLResponse:
    rows = db.scalars(select(Job).order_by(Job.id.desc()).limit(100)).all()
    active = any(j.status in jobs.ACTIVE for j in rows)
    return render(request, "jobs.html", settings, jobs=rows, refresh=3 if active else None)


# --- wiring --------------------------------------------------------------------------


def install(app: FastAPI) -> None:
    app.include_router(public)
    app.include_router(router)

    @app.middleware("http")
    async def admin_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        if request.url.path.startswith("/admin"):
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                "frame-ancestors 'none'"
            )
        return response


def recover_jobs(factory: sessionmaker[Session]) -> None:
    with factory() as session:
        jobs.mark_interrupted(session)
