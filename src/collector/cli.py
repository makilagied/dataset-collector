from datetime import timedelta

import httpx
import typer
from sqlalchemy import select

from collector.accounts import refresh_expiring
from collector.db import session_scope
from collector.models import Account

app = typer.Typer(help="Swahili abusive-comment dataset collector", no_args_is_help=True)
accounts_app = typer.Typer(help="Connected Instagram accounts", no_args_is_help=True)
tokens_app = typer.Typer(help="Access token maintenance", no_args_is_help=True)
app.add_typer(accounts_app, name="accounts")
app.add_typer(tokens_app, name="tokens")


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
