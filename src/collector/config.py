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
    graph_api_version: str = "v25.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
