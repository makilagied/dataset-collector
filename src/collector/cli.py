import logging
from datetime import datetime, timedelta
from typing import Annotated

import httpx
import typer
from sqlalchemy import select

from collector.accounts import refresh_expiring
from collector.config import get_settings
from collector.db import session_scope
from collector.ig import oauth
from collector.ig.backfill import backfill_account
from collector.ig.client import GraphClient
from collector.models import Account
from collector.security import decrypt_token

app = typer.Typer(help="Swahili abusive-comment dataset collector", no_args_is_help=True)
accounts_app = typer.Typer(help="Connected Instagram accounts", no_args_is_help=True)
tokens_app = typer.Typer(help="Access token maintenance", no_args_is_help=True)
app.add_typer(accounts_app, name="accounts")
app.add_typer(tokens_app, name="tokens")
webhooks_app = typer.Typer(help="Webhook subscriptions", no_args_is_help=True)
app.add_typer(webhooks_app, name="webhooks")


@accounts_app.command("list")
def accounts_list() -> None:
    """List connected accounts."""
    with session_scope() as session:
        rows = session.scalars(select(Account).order_by(Account.id)).all()
        if not rows:
            typer.echo("No accounts connected.")
            return
        for a in rows:
            expires = a.token_expires_at.date().isoformat() if a.token_expires_at else "-"
            status = "active" if a.active else "inactive"
            typer.echo(f"{a.id:>4}  @{a.username or '?':<30} {status:<8} token expires {expires}")


@app.command()
def backfill(
    account: Annotated[int, typer.Option(help="Local account id (see `accounts list`)")],
    since: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Only media posted on/after")
    ] = None,
) -> None:
    """Fetch media, comments and replies for one account. Safe to rerun; resumes if interrupted."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    with session_scope() as session, httpx.Client(timeout=30) as http:
        acct = session.get(Account, account)
        if acct is None or not acct.active:
            typer.echo(f"No active account with id {account}.", err=True)
            raise typer.Exit(1)
        client = GraphClient(
            decrypt_token(acct.access_token_enc), http=http, version=settings.graph_api_version
        )
        stats = backfill_account(session, client, acct, since.date() if since else None)
    typer.echo(
        f"media: {stats.media_seen} seen, {stats.media_skipped} skipped (resumed) | "
        f"comments: {stats.comments_seen} seen, {stats.comments_inserted} new"
    )


@webhooks_app.command("subscribe")
def webhooks_subscribe(
    account: Annotated[int, typer.Option(help="Local account id (see `accounts list`)")],
) -> None:
    """(Re)enable comments/live_comments webhook delivery for an account."""
    settings = get_settings()
    with session_scope() as session, httpx.Client(timeout=30) as http:
        acct = session.get(Account, account)
        if acct is None:
            typer.echo(f"No account with id {account}.", err=True)
            raise typer.Exit(1)
        try:
            oauth.subscribe_webhooks(http, settings, decrypt_token(acct.access_token_enc))
        except oauth.OAuthError as e:
            typer.echo(f"Failed: {e}", err=True)
            raise typer.Exit(1) from e
    typer.echo(f"@{acct.username}: subscribed to {', '.join(oauth.WEBHOOK_FIELDS)}")


@tokens_app.command("refresh")
def tokens_refresh(
    days: int = typer.Option(10, help="Refresh tokens expiring within N days"),
) -> None:
    """Refresh long-lived tokens that expire soon."""
    with session_scope() as session, httpx.Client(timeout=30) as client:
        results = refresh_expiring(session, client, timedelta(days=days))
        if not results:
            typer.echo("No tokens need refreshing.")
        for account, err in results:
            typer.echo(f"@{account.username}: {'FAILED ' + err if err else 'refreshed'}")
        if any(err for _, err in results):
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
