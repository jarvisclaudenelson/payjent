from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYJENT_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./payjent.db"
    signing_secret: str = "dev-only-change-me"
    dev_mode: bool = True
    mock_provider_enabled: bool = True
    checkout_provider: str = "mock"
    public_base_url: str | None = None
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_success_url_template: str | None = None
    stripe_cancel_url_template: str | None = None
    grant_ttl_seconds: int = 900


@lru_cache
def get_settings() -> Settings:
    return Settings()
