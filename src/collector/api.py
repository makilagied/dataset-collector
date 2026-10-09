import json
import logging
from collections.abc import Iterator
from functools import lru_cache
from html import escape
from typing import Annotated, Any

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from collector.accounts import onboard
from collector.config import Settings, get_settings
from collector.db import make_engine
from collector.ig import oauth, webhook

logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(title="IG abuse dataset collector")


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(make_engine(), expire_on_commit=False)


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
    except oauth.OAuthError as e:
        raise HTTPException(status_code=400, detail="invalid state") from e
    if error or not code:
        raise HTTPException(status_code=400, detail=f"authorization failed: {error or 'no code'}")
    try:
        account = onboard(session, http, settings, code, consent_ref)
    except oauth.OAuthError as e:
        raise HTTPException(status_code=502, detail="token exchange failed") from e
    return HTMLResponse(
        f"<h1>Connected</h1><p>Thank you. @{escape(account.username or '')} is now connected."
        " You can close this page.</p>"
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
        "webhook: %d new, %d duplicate, %d skipped %s",
        stats.inserted,
        stats.duplicates,
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
