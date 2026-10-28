import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from sqlalchemy import select

from collector import net
from collector.accounts import refresh_expiring
from collector.config import get_settings
from collector.db import session_scope
from collector.ig import oauth
from collector.ig.backfill import backfill_account
from collector.ig.client import GraphClient
from collector.logs import configure_logging
from collector.models import Account
from collector.security import decrypt_token

if TYPE_CHECKING:
    from collector.scrape.run import ScrapeOptions

app = typer.Typer(help="Swahili abusive-comment dataset collector", no_args_is_help=True)
accounts_app = typer.Typer(help="Connected Instagram accounts", no_args_is_help=True)
tokens_app = typer.Typer(help="Access token maintenance", no_args_is_help=True)
app.add_typer(accounts_app, name="accounts")
app.add_typer(tokens_app, name="tokens")
webhooks_app = typer.Typer(help="Webhook subscriptions", no_args_is_help=True)
app.add_typer(webhooks_app, name="webhooks")
demo_app = typer.Typer(help="Synthetic demo data (no Meta app needed)", no_args_is_help=True)
app.add_typer(demo_app, name="demo")
scrape_app = typer.Typer(
    help="Scrape public Instagram/Facebook comments with a logged-in browser",
    no_args_is_help=True,
)
app.add_typer(scrape_app, name="scrape")


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
            typer.echo(
                f"{a.id:>4}  @{a.username or '?':<30} {status:<8} "
                f"category {a.category or '-':<10} token expires {expires}"
            )


@app.command()
def backfill(
    account: Annotated[int, typer.Option(help="Local account id (see `accounts list`)")],
    since: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Only media posted on/after")
    ] = None,
) -> None:
    """Fetch media, comments and replies for one account. Safe to rerun; resumes if interrupted."""
    configure_logging(logging.INFO)
    settings = get_settings()
    with session_scope() as session, net.http_client() as http:
        acct = session.get(Account, account)
        if acct is None or not acct.active:
            typer.echo(f"No active account with id {account}.", err=True)
            raise typer.Exit(1)
        if acct.ig_user_id.startswith("scrape:"):
            typer.echo(f"{acct.username} is a scraped source; use `collector scrape`.", err=True)
            raise typer.Exit(1)
        client = GraphClient(
            decrypt_token(acct.access_token_enc), http=http, version=settings.graph_api_version
        )
        stats = backfill_account(session, client, acct, since.date() if since else None)
    typer.echo(
        f"media: {stats.media_seen} seen, {stats.media_skipped} skipped (resumed) | "
        f"comments: {stats.comments_seen} seen, {stats.comments_inserted} new, "
        f"{stats.comments_dropped} dropped (no abusive word)"
    )


@app.command()
def process(
    batch_size: Annotated[int, typer.Option(help="Comments per batch")] = 500,
    all_: Annotated[
        bool, typer.Option("--all", help="Reprocess every comment, not just new/outdated ones")
    ] = False,
) -> None:
    """Normalize, match and language-ID new comments into comment_features."""
    from collector.pipeline.process import pipeline_version
    from collector.pipeline.process import process as run_process

    typer.echo(f"pipeline version {pipeline_version()}")
    with session_scope() as session:
        stats = run_process(session, batch_size=batch_size, reprocess_all=all_)
    typer.echo(f"processed {stats.processed} comments, {stats.hits} lexicon hits")
    if stats.excluded:
        excluded = ", ".join(f"{k} {v}" for k, v in stats.excluded.most_common())
        typer.echo(f"excluded from sampling: {excluded}")
    if stats.langs:
        typer.echo("languages: " + ", ".join(f"{k} {v}" for k, v in stats.langs.most_common()))


@app.command("trim-long")
def trim_long(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only count what would change")
    ] = False,
) -> None:
    """Cut stored comments longer than 200 characters down to the 150 characters around
    their hit word; delete long ones with no hit word. Run `process` afterwards."""
    from collector.pipeline.trim import trim_long_comments

    with session_scope() as session:
        stats = trim_long_comments(session, dry_run=dry_run)
    verb = "would be" if dry_run else "were"
    typer.echo(f"{stats.trimmed} long comments {verb} cut to an excerpt around the hit word")
    typer.echo(f"{stats.deleted} long comments with no hit word {verb} deleted")
    if stats.trimmed and not dry_run:
        if get_settings().dedupe_hits:
            dedupe(dry_run=False)  # an excerpt may now share its hit words with another
        typer.echo("run `collector process` to redo their features")


@app.command()
def prune(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only count what would be deleted")
    ] = False,
) -> None:
    """Delete stored comments that are no longer lexicon hits (STORE_ONLY_HITS), e.g.
    after adding a word to the allowlist."""
    from collector.pipeline.trim import prune_non_hits

    with session_scope() as session:
        stats = prune_non_hits(session, dry_run=dry_run)
    verb = "would be" if dry_run else "were"
    typer.echo(
        f"{stats.deleted} comments that are no longer hits {verb} deleted"
        f" ({stats.occurrences} with their repeats)"
    )


@app.command()
def dedupe(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Only count what would change")
    ] = False,
) -> None:
    """Keep one comment per set of hit words: merge the others into the earliest one,
    adding their counts to its occurrences. Run `process` afterwards."""
    from collector.pipeline.dedupe import dedupe_all

    with session_scope() as session:
        stats = dedupe_all(session, dry_run=dry_run)
    verb = "would be" if dry_run else "were"
    typer.echo(f"{stats.kept} comments kept, {stats.merged} duplicates {verb} merged into them")
    if stats.merged and not dry_run:
        typer.echo("run `collector process` to recount hit words")


@app.command()
def sample(
    batch: Annotated[str, typer.Option(help="Name for the new labelling batch")],
    size: Annotated[int, typer.Option(help="Number of comments to draw")],
    mix: Annotated[str, typer.Option(help="lexicon/model/random percentages")] = "50/30/20",
    scorer: Annotated[str, typer.Option(help="Scorer for the model stratum")] = "stub",
    seed: Annotated[int | None, typer.Option(help="Seed for a reproducible draw")] = None,
    lang: Annotated[
        str | None, typer.Option(help="Only comments in this language, e.g. sw")
    ] = None,
) -> None:
    """Draw a stratified batch of comments for labelling."""
    from collector.pipeline.sample import SampleError, parse_mix, sample_batch
    from collector.pipeline.scoring import SCORERS

    if scorer not in SCORERS:
        typer.echo(f"Unknown scorer {scorer!r}; choose from {', '.join(SCORERS)}", err=True)
        raise typer.Exit(1)
    try:
        with session_scope() as session:
            result = sample_batch(
                session,
                batch,
                size,
                parse_mix(mix),
                scorer=SCORERS[scorer](),
                seed=seed,
                lang=lang,
            )
    except SampleError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    parts = ", ".join(f"{s} {result.drawn[s]}/{result.requested[s]}" for s in result.drawn)
    typer.echo(f"batch {batch!r}: {result.total} comments ({parts})")
    for note in result.notes:
        typer.echo(f"  note: {note}")


@app.command()
def export(
    batch: Annotated[str, typer.Option(help="Batch to export")],
    format_: Annotated[str, typer.Option("--format", help="csv or jsonl")] = "csv",
    out: Annotated[Path | None, typer.Option(help="Output file")] = None,
) -> None:
    """Export a batch for labelling (mentions, URLs and emails masked)."""
    from collector.pipeline.export import ExportError, export_batch

    path = out or Path("exports") / f"{batch}.{format_}"
    try:
        with session_scope() as session:
            result = export_batch(session, batch, format_, path)
    except ExportError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"wrote {result.rows} rows to {result.path}")


@app.command("export-all")
def export_all(
    format_: Annotated[str, typer.Option("--format", help="csv or jsonl")] = "csv",
    account: Annotated[int | None, typer.Option(help="Only this account id")] = None,
    show: Annotated[
        str, typer.Option(help="all | hits | matched | excluded | unprocessed")
    ] = "all",
    out: Annotated[Path | None, typer.Option(help="Output file")] = None,
) -> None:
    """Export ALL collected comments (abusive or not), anonymized, with their features."""
    from collector.pipeline.export import ExportError, export_all_comments

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = out or Path("exports") / f"comments-{show}-{stamp}.{format_}"
    try:
        with session_scope() as session:
            result = export_all_comments(session, format_, path, account_id=account, show=show)
    except ExportError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"wrote {result.rows} comments to {result.path}")


@app.command("export-words")
def export_words(
    format_: Annotated[str, typer.Option("--format", help="csv or jsonl")] = "csv",
    q: Annotated[str, typer.Option(help="Only roots/spellings containing this")] = "",
    out: Annotated[Path | None, typer.Option(help="Output file")] = None,
) -> None:
    """Export the hit words: every spelling seen and how many comments contain it."""
    from collector.pipeline.export import ExportError, export_hit_words

    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = out or Path("exports") / f"hit-words-{stamp}.{format_}"
    try:
        with session_scope() as session:
            result = export_hit_words(session, format_, path, q=q)
    except ExportError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    typer.echo(f"wrote {result.rows} words to {result.path}")


@accounts_app.command("set-category")
def accounts_set_category(
    account: Annotated[int, typer.Argument(help="Local account id")],
    category: Annotated[str, typer.Argument(help="e.g. udaku, football, media, music")],
) -> None:
    """Set the category exported with this account's comments."""
    with session_scope() as session:
        acct = session.get(Account, account)
        if acct is None:
            typer.echo(f"No account with id {account}.", err=True)
            raise typer.Exit(1)
        acct.category = category.strip().lower()
    typer.echo(f"account {account}: category = {category.strip().lower()}")


@app.command()
def stats() -> None:
    """Counts per account, abuse-hit rate, language split, exclusions and batches."""
    from collector.pipeline.stats import collect_stats

    with session_scope() as session:
        s = collect_stats(session)
    if not s.accounts:
        typer.echo("No accounts yet. Connect one via /connect, or try `collector demo load`.")
        return
    typer.echo(
        f"{'id':>4}  {'account':<22} {'category':<10} {'media':>6} {'comments':>9} "
        f"{'processed':>10} {'eligible':>9} {'hits':>6} {'hit rate':>9}"
    )
    for a in s.accounts:
        name = ("@" + a.username + ("" if a.active else " (inactive)"))[:22]
        typer.echo(
            f"{a.account_id:>4}  {name:<22} {a.category[:10]:<10} {a.media:>6} "
            f"{a.comments:>9} {a.processed:>10} {a.eligible:>9} {a.hits:>6} "
            f"{a.hit_rate:>8.1%}"
        )
    typer.echo(
        f"{'':>4}  {'TOTAL':<22} {'':<10} {sum(a.media for a in s.accounts):>6} "
        f"{s.comments:>9} {s.processed:>10} {s.eligible:>9} {s.hits:>6} "
        f"{s.hit_rate:>8.1%}"
    )
    if s.comments > s.processed:
        typer.echo(
            f"\n{s.comments - s.processed} comments not processed yet: run `collector process`."
        )

    def shares(counts: dict[str, int]) -> str:
        total = sum(counts.values()) or 1
        return ", ".join(f"{k} {v} ({v / total:.0%})" for k, v in counts.items())

    if s.languages:
        typer.echo(f"\nlanguages (eligible): {shares(s.languages)}")
    if s.excluded:
        typer.echo(
            "excluded from sampling: " + ", ".join(f"{k} {v}" for k, v in s.excluded.items())
        )
    if s.batches:
        typer.echo("\nbatches:")
        for b in s.batches:
            strata = ", ".join(f"{k} {v}" for k, v in sorted(b.strata.items()))
            typer.echo(f"  {b.name:<20} {b.size:>5} ({strata}){'  exported' if b.exported else ''}")
    typer.echo("\nhit = lexicon match_score >= 0.5, over comments eligible for sampling")


@demo_app.command("load")
def demo_load() -> None:
    """Backfill a synthetic demo account through a fake, in-process Instagram API."""
    from collector.demo import load_demo

    configure_logging(logging.WARNING)
    with session_scope() as session:
        account, stats = load_demo(session)
    typer.echo(
        f"demo account id {account.id}: {stats.media_seen} posts, "
        f"{stats.comments_seen} comments seen, {stats.comments_inserted} new"
    )
    typer.echo("next: collector process && collector stats")


@demo_app.command("remove")
def demo_remove() -> None:
    """Delete the demo account and all of its data."""
    from collector.demo import remove_demo

    with session_scope() as session:
        removed = remove_demo(session)
    typer.echo(
        f"removed demo account and {removed} comments" if removed else "no demo account found"
    )


@webhooks_app.command("subscribe")
def webhooks_subscribe(
    account: Annotated[int, typer.Option(help="Local account id (see `accounts list`)")],
) -> None:
    """(Re)enable comments/live_comments webhook delivery for an account."""
    settings = get_settings()
    with session_scope() as session, net.http_client() as http:
        acct = session.get(Account, account)
        if acct is None or acct.ig_user_id.startswith("scrape:"):
            typer.echo(f"No connected account with id {account}.", err=True)
            raise typer.Exit(1)
        try:
            oauth.subscribe_webhooks(http, settings, decrypt_token(acct.access_token_enc))
        except oauth.OAuthError as e:
            typer.echo(f"Failed: {e}", err=True)
            raise typer.Exit(1) from e
    typer.echo(f"@{acct.username}: subscribed to {', '.join(oauth.WEBHOOK_FIELDS)}")


Targets = Annotated[list[str], typer.Argument(help="Profiles, pages, #hashtags or post URLs")]
MaxPosts = Annotated[int, typer.Option(help="Posts per target")]
MaxComments = Annotated[int, typer.Option(help="Comments per post")]
Category = Annotated[str | None, typer.Option(help="Category stored with these sources")]
Delay = Annotated[float, typer.Option(help="Average seconds between requests")]
Refresh = Annotated[bool, typer.Option(help="Rescrape posts already scraped")]
OnlyHits = Annotated[
    bool | None,
    typer.Option(
        "--only-hits/--everything",
        help="Store only comments matching the lexicon keywords, or every comment "
        "(default: STORE_ONLY_HITS)",
    ),
]


@scrape_app.command("login")
def scrape_login(
    platform: Annotated[str, typer.Argument(help="instagram, facebook, tiktok or x")],
) -> None:
    """Open a browser window to log in by hand. The session is kept for later scrapes."""
    from collector.scrape.run import LOGIN_TIMEOUT, wait_for_login

    if platform not in ("instagram", "facebook", "tiktok", "x"):
        typer.echo("platform must be instagram, facebook, tiktok or x", err=True)
        raise typer.Exit(1)
    typer.echo(
        f"Log in in the browser window. It closes by itself once you are in "
        f"(or after {LOGIN_TIMEOUT / 60:.0f} minutes)."
    )
    with session_scope() as session:
        ok = wait_for_login(session, platform)
    typer.echo(f"{platform}: {'logged in, session saved' if ok else 'NOT logged in'}")
    if not ok:
        raise typer.Exit(1)


def _run_scrape(platform: str, targets: list[str], options: "ScrapeOptions") -> None:
    from collector.scrape.common import ScrapeError
    from collector.scrape.run import run_scrape

    configure_logging(logging.INFO)
    try:
        with session_scope() as session:
            run = run_scrape(session, platform, targets, options)
    except ScrapeError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1) from e
    for result in run.results:
        typer.echo(str(result), err=result.stats is None)
    if run.blocked:
        typer.echo(f"Stopped: {run.blocked}. Everything scraped so far is saved.", err=True)
        raise typer.Exit(2)
    typer.echo("next: collector process && collector stats")
    if run.failed:
        raise typer.Exit(1)


@scrape_app.command("instagram")
def scrape_instagram(
    targets: Targets,
    max_posts: MaxPosts = 20,
    max_comments: MaxComments = 5000,
    since: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Only posts on/after")
    ] = None,
    category: Category = None,
    delay: Delay = 4.0,
    refresh: Refresh = False,
    only_hits: OnlyHits = None,
) -> None:
    """Scrape comments from @users, #hashtags or post URLs."""
    from collector.scrape.run import ScrapeOptions

    options = ScrapeOptions(
        max_posts=max_posts,
        max_comments=max_comments,
        since=since.date() if since else None,
        category=category,
        delay=delay,
        refresh=refresh,
        only_hits=only_hits,
    )
    _run_scrape("instagram", targets, options)


@scrape_app.command("facebook")
def scrape_facebook(
    targets: Targets,
    max_posts: MaxPosts = 20,
    max_comments: MaxComments = 5000,
    category: Category = None,
    delay: Delay = 4.0,
    refresh: Refresh = False,
    only_hits: OnlyHits = None,
) -> None:
    """Scrape comments from page / group URLs (recent posts) or post URLs."""
    from collector.scrape.run import ScrapeOptions

    options = ScrapeOptions(
        max_posts=max_posts,
        max_comments=max_comments,
        category=category,
        delay=delay,
        refresh=refresh,
        only_hits=only_hits,
    )
    _run_scrape("facebook", targets, options)


@scrape_app.command("tiktok")
def scrape_tiktok(
    targets: Targets,
    max_posts: MaxPosts = 20,
    max_comments: MaxComments = 5000,
    since: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Only videos on/after")
    ] = None,
    category: Category = None,
    delay: Delay = 4.0,
    refresh: Refresh = False,
    only_hits: OnlyHits = None,
) -> None:
    """Scrape comments from @users, #hashtags or video URLs. No login needed (log in
    with `scrape login tiktok` if TikTok starts asking). Keep the browser window visible:
    if TikTok shows a captcha, solve it there and the scrape carries on."""
    from collector.scrape.run import ScrapeOptions

    options = ScrapeOptions(
        max_posts=max_posts,
        max_comments=max_comments,
        since=since.date() if since else None,
        category=category,
        delay=delay,
        refresh=refresh,
        only_hits=only_hits,
    )
    _run_scrape("tiktok", targets, options)


@scrape_app.command("x")
def scrape_x(
    targets: Targets,
    max_posts: MaxPosts = 20,
    max_comments: MaxComments = 5000,
    since: Annotated[
        datetime | None, typer.Option(formats=["%Y-%m-%d"], help="Only posts on/after")
    ] = None,
    category: Category = None,
    delay: Delay = 4.0,
    refresh: Refresh = False,
    only_hits: OnlyHits = None,
) -> None:
    """Scrape replies to posts on X (formerly Twitter) from @users, #hashtags or post URLs.
    Log in first with `scrape login x`. If X rate limits, the scrape pauses and carries on."""
    from collector.scrape.run import ScrapeOptions

    options = ScrapeOptions(
        max_posts=max_posts,
        max_comments=max_comments,
        since=since.date() if since else None,
        category=category,
        delay=delay,
        refresh=refresh,
        only_hits=only_hits,
    )
    _run_scrape("x", targets, options)


@app.command()
def ui(
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8001,
) -> None:
    """Run the admin dashboard on this computer (needed for scraping from the UI)."""
    import uvicorn

    typer.echo(f"Dashboard: http://127.0.0.1:{port}/admin  (Ctrl+C to stop)")
    uvicorn.run("collector.api:app", host="127.0.0.1", port=port)


@tokens_app.command("refresh")
def tokens_refresh(
    days: int = typer.Option(10, help="Refresh tokens expiring within N days"),
) -> None:
    """Refresh long-lived tokens that expire soon."""
    with session_scope() as session, net.http_client() as client:
        results = refresh_expiring(session, client, timedelta(days=days))
        if not results:
            typer.echo("No tokens need refreshing.")
        for account, err in results:
            typer.echo(f"@{account.username}: {'FAILED ' + err if err else 'refreshed'}")
        if any(err for _, err in results):
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
