from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All values come from the environment. Secrets have no defaults."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    ig_app_id: str
    ig_app_secret: str
    ig_redirect_uri: str
    ig_webhook_verify_token: str
    token_enc_key: str
    author_hash_key: str
    # Only needed if webhooks also arrive from a Facebook Login app (signed with its secret).
    fb_app_secret: str | None = None
    graph_api_version: str = "v25.0"
    # Admin UI at /admin. Disabled while ADMIN_PASSWORD is empty. Requests through a proxy
    # or tunnel (ngrok) are refused unless ADMIN_ALLOW_REMOTE is true.
    admin_user: str = "admin"
    admin_password: str | None = None
    admin_allow_remote: bool = False
    # Store only comments with a Swahili/English abusive word (a lexicon hit), from every
    # source: API backfill, webhooks and scraping. Others are read and dropped.
    store_only_hits: bool = True
    # Store one comment per set of hit words: a comment whose hit words are exactly those of
    # a stored comment is not stored; the stored one's `occurrences` goes up instead.
    dedupe_hits: bool = True
    # Browser profile holding the logged-in Instagram/Facebook sessions used by `scrape`.
    scrape_profile_dir: str = ".scrape-profile"
    # Most paced requests a scrape makes in any rolling hour (0 = no cap). Instagram rate
    # limits a session (HTTP 429) after many hours at full speed.
    scrape_max_requests_per_hour: int = 600


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
