"""Business Login for Instagram: authorize URL, code exchange, long-lived token, refresh.

Endpoints checked October 2026 against
https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from collector.config import Settings

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
SHORT_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_HOST = "https://graph.instagram.com"
SCOPES = ("instagram_business_basic", "instagram_business_manage_comments")
STATE_MAX_AGE_S = 600


class OAuthError(Exception):
    pass


@dataclass(frozen=True)
class LongLivedToken:
    access_token: str
    expires_at: datetime


@dataclass(frozen=True)
class Profile:
    user_id: str  # Instagram professional account ID; matches webhook entry id
    username: str


# --- signed state -----------------------------------------------------------


def _state_key(settings: Settings) -> bytes:
    # Derived from the app secret so no extra env var is needed.
    return hmac.new(settings.ig_app_secret.encode(), b"oauth-state", hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def make_state(settings: Settings, consent_ref: str | None = None, now: float | None = None) -> str:
    payload = {"n": secrets.token_urlsafe(12), "t": int(now or time.time()), "c": consent_ref}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_state_key(settings), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_state(settings: Settings, state: str, now: float | None = None) -> str | None:
    """Return the consent_ref carried in the state, or raise OAuthError."""
    try:
        body, sig = state.split(".", 1)
    except ValueError as e:
        raise OAuthError("malformed state") from e
    expected = _b64(hmac.new(_state_key(settings), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise OAuthError("bad state signature")
    payload = json.loads(_unb64(body))
    if (now or time.time()) - payload["t"] > STATE_MAX_AGE_S:
        raise OAuthError("state expired")
    consent_ref: str | None = payload.get("c")
    return consent_ref


# --- OAuth calls ------------------------------------------------------------


def authorize_url(settings: Settings, state: str) -> str:
    params = {
        "client_id": settings.ig_app_id,
        "redirect_uri": settings.ig_redirect_uri,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
        "enable_fb_login": "false",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def _check(resp: httpx.Response) -> dict[str, object]:
    if resp.status_code != 200:
        raise OAuthError(f"{resp.request.url.path}: HTTP {resp.status_code}: {resp.text[:300]}")
    data: dict[str, object] = resp.json()
    return data


def exchange_code(client: httpx.Client, settings: Settings, code: str) -> str:
    """Exchange the authorization code for a short-lived token."""
    code = code.removesuffix("#_")
    data = _check(
        client.post(
            SHORT_TOKEN_URL,
            data={
                "client_id": settings.ig_app_id,
                "client_secret": settings.ig_app_secret,
                "grant_type": "authorization_code",
                "redirect_uri": settings.ig_redirect_uri,
                "code": code,
            },
        )
    )
    # Documented shape is {"data": [{...}]}; older responses were flat.
    entries = data.get("data")
    entry = entries[0] if isinstance(entries, list) and entries else data
    if not isinstance(entry, dict) or "access_token" not in entry:
        raise OAuthError("no access_token in code exchange response")
    return str(entry["access_token"])


def _long_lived(data: dict[str, object]) -> LongLivedToken:
    expires_in = int(str(data.get("expires_in", 0)))
    return LongLivedToken(
        access_token=str(data["access_token"]),
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )


def exchange_long_lived(
    client: httpx.Client, settings: Settings, short_token: str
) -> LongLivedToken:
    data = _check(
        client.get(
            f"{GRAPH_HOST}/access_token",
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": settings.ig_app_secret,
                "access_token": short_token,
            },
        )
    )
    return _long_lived(data)


def refresh_long_lived(client: httpx.Client, token: str) -> LongLivedToken:
    data = _check(
        client.get(
            f"{GRAPH_HOST}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": token},
        )
    )
    return _long_lived(data)


WEBHOOK_FIELDS = ("comments", "live_comments")


def subscribe_webhooks(client: httpx.Client, settings: Settings, token: str) -> None:
    """Enable webhook delivery for this account (POST /me/subscribed_apps)."""
    data = _check(
        client.post(
            f"{GRAPH_HOST}/{settings.graph_api_version}/me/subscribed_apps",
            params={"subscribed_fields": ",".join(WEBHOOK_FIELDS), "access_token": token},
        )
    )
    if data.get("success") is not True:
        raise OAuthError(f"subscribed_apps did not report success: {data}")


def fetch_profile(client: httpx.Client, settings: Settings, token: str) -> Profile:
    data = _check(
        client.get(
            f"{GRAPH_HOST}/{settings.graph_api_version}/me",
            params={"fields": "user_id,username", "access_token": token},
        )
    )
    return Profile(user_id=str(data["user_id"]), username=str(data.get("username", "")))
