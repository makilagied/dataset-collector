import json
import logging
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from html import escape
from typing import Annotated, Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from collector import admin
from collector.accounts import onboard
from collector.config import Settings, get_settings
from collector.db import get_sessionmaker
from collector.ig import oauth, webhook
from collector.logs import configure_logging

configure_logging()
log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        admin.recover_jobs(get_sessionmaker())
    except Exception:  # never block startup on this bookkeeping
        log.warning("could not mark interrupted jobs", exc_info=True)
    yield


app = FastAPI(title="IG abuse dataset collector", lifespan=lifespan)
admin.install(app)

SessionmakerDep = Annotated[sessionmaker[Session], Depends(get_sessionmaker)]


def get_session(factory: SessionmakerDep) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=30) as client:
        yield client


SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionDep = Annotated[Session, Depends(get_session)]
HttpDep = Annotated[httpx.Client, Depends(get_http)]


@app.get("/connect")
def connect(settings: SettingsDep, consent_ref: str | None = None) -> RedirectResponse:
    state = oauth.make_state(settings, consent_ref)
    return RedirectResponse(oauth.authorize_url(settings, state), status_code=302)


@app.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(
    settings: SettingsDep,
    session: SessionDep,
    http: HttpDep,
    state: Annotated[str, Query()] = "",
    code: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    try:
        consent_ref = oauth.verify_state(settings, state)
    except oauth.OAuthError:
        # Usually a reloaded/restored callback tab or a login that took over 10 minutes.
        return _page(
            "This link has expired",
            'Please <a href="/connect">start the connection again</a>.',
            status=400,
        )
    if error or not code:
        return _page(
            "Connection cancelled",
            'Instagram did not grant access. <a href="/connect">Try again</a> if this was a '
            "mistake.",
            status=400,
        )
    try:
        account = onboard(session, http, settings, code, consent_ref)
    except oauth.OAuthError:
        log.warning("token exchange failed", exc_info=True)
        return _page(
            "Something went wrong",
            'We could not complete the connection with Instagram. Please <a href="/connect">'
            "try again</a> in a few minutes.",
            status=502,
        )
    return _page(
        "Connected",
        f"Thank you. @{escape(account.username or '')} is now connected. You can close this page.",
    )


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>{escape(title)}</title>"
        f"<body style='font-family:system-ui,sans-serif;max-width:32rem;margin:4rem auto;"
        f"padding:0 1rem;line-height:1.5'><h1>{escape(title)}</h1><p>{body}</p></body>",
        status_code=status,
    )


@app.get("/webhook", response_class=PlainTextResponse)
def webhook_verify(
    settings: SettingsDep,
    mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
) -> str:
    if mode == "subscribe" and token == settings.ig_webhook_verify_token and challenge:
        return challenge
    raise HTTPException(status_code=403, detail="verification failed")


def _ingest_in_background(factory: sessionmaker[Session], payload: dict[str, Any]) -> None:
    with factory() as session:
        try:
            stats = webhook.ingest(session, payload)
        except Exception:
            session.rollback()
            log.exception("webhook ingestion failed")
            return
    log.info(
        "webhook: %d new, %d duplicate, %d dropped (no hit), %d skipped %s",
        stats.inserted,
        stats.duplicates,
        stats.dropped,
        stats.skipped,
        stats.reasons or "",
    )


@app.post("/webhook")
async def webhook_receive(
    request: Request,
    background: BackgroundTasks,
    settings: SettingsDep,
    factory: SessionmakerDep,
) -> dict[str, str]:
    body = await request.body()
    secrets = [s for s in (settings.ig_app_secret, settings.fb_app_secret) if s]
    if not webhook.verify_signature(body, request.headers.get("x-hub-signature-256"), secrets):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        payload = json.loads(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="invalid JSON") from e
    # Acknowledge fast; Meta retries unacknowledged deliveries.
    background.add_task(_ingest_in_background, factory, payload)
    return {"status": "ok"}
