from collections.abc import Iterator
from html import escape
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, sessionmaker

from collector.accounts import onboard
from collector.config import Settings, get_settings
from collector.db import make_engine
from collector.ig import oauth

app = FastAPI(title="IG abuse dataset collector")

_sessionmaker: sessionmaker[Session] | None = None


def get_session() -> Iterator[Session]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = sessionmaker(make_engine(), expire_on_commit=False)
    session = _sessionmaker()
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
