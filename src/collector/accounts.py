import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from collector.config import Settings
from collector.ig import oauth
from collector.models import Account
from collector.security import decrypt_token, encrypt_token

log = logging.getLogger(__name__)


def onboard(
    session: Session,
    client: httpx.Client,
    settings: Settings,
    code: str,
    consent_ref: str | None,
) -> Account:
    """Run the code -> short-lived -> long-lived exchange and upsert the account."""
    short = oauth.exchange_code(client, settings, code)
    long = oauth.exchange_long_lived(client, settings, short)
    profile = oauth.fetch_profile(client, settings, long.access_token)

    values = {
        "ig_user_id": profile.user_id,
        "username": profile.username,
        "access_token_enc": encrypt_token(long.access_token),
        "token_expires_at": long.expires_at,
        "active": True,
    }
    stmt = insert(Account).values(consent_ref=consent_ref, **values)
    update = dict(values)
    if consent_ref is not None:
        update["consent_ref"] = consent_ref
    stmt = stmt.on_conflict_do_update(index_elements=[Account.ig_user_id], set_=update)
    session.execute(stmt)
    session.flush()

    # Best effort: a failed subscription shouldn't undo onboarding; it can be retried
    # with `collector webhooks subscribe`. Backfill still works without it.
    try:
        oauth.subscribe_webhooks(client, settings, long.access_token)
    except (oauth.OAuthError, httpx.HTTPError) as e:
        log.warning("webhook subscription failed for %s: %s", profile.user_id, e)

    return session.scalars(select(Account).where(Account.ig_user_id == profile.user_id)).one()


def refresh_expiring(
    session: Session, client: httpx.Client, within: timedelta = timedelta(days=10)
) -> list[tuple[Account, str | None]]:
    """Refresh every active token expiring within `within`. Returns (account, error) pairs."""
    cutoff = datetime.now(UTC) + within
    accounts = session.scalars(
        select(Account).where(Account.active.is_(True), Account.token_expires_at < cutoff)
    ).all()
    results: list[tuple[Account, str | None]] = []
    for account in accounts:
        try:
            token = oauth.refresh_long_lived(client, decrypt_token(account.access_token_enc))
        except oauth.OAuthError as e:
            results.append((account, str(e)))
            continue
        account.access_token_enc = encrypt_token(token.access_token)
        account.token_expires_at = token.expires_at
        results.append((account, None))
    session.flush()
    return results
