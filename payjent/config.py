from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SIGNING_SECRET = "dev-only-change-me"
PRODUCTION_ENV_NAMES = {"prod", "production"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAYJENT_", env_file=".env", extra="ignore")

    env: str = "local"
    database_url: str = "sqlite:///./payjent.db"
    signing_secret: str = DEFAULT_SIGNING_SECRET
    dev_mode: bool = True
    mock_provider_enabled: bool = True
    checkout_provider: str = "mock"
    public_base_url: str | None = None
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_success_url_template: str | None = None
    stripe_cancel_url_template: str | None = None
    workos_api_key: str | None = None
    workos_client_id: str | None = None
    workos_redirect_uri: str | None = None
    grant_ttl_seconds: int = 900
    allow_unsafe_db_reset: bool = False
    bootstrap_token: str | None = None

    @property
    def is_production(self) -> bool:
        return self.env.strip().lower() in PRODUCTION_ENV_NAMES

    @property
    def effective_mock_provider_enabled(self) -> bool:
        return self.dev_mode and self.mock_provider_enabled and not self.is_production

    def validate_runtime_guardrails(self) -> None:
        """Fail closed for unsafe production configuration before serving traffic."""
        if not self.is_production:
            return

        problems: list[str] = []
        if self.signing_secret == DEFAULT_SIGNING_SECRET or self.signing_secret.startswith("dev-"):
            problems.append("PAYJENT_SIGNING_SECRET must be non-default in production")
        if not self.public_base_url or not self.public_base_url.startswith("https://"):
            problems.append("PAYJENT_PUBLIC_BASE_URL must be an https:// URL in production")
        if self.checkout_provider.lower() == "stripe":
            if not self.stripe_secret_key:
                problems.append("PAYJENT_STRIPE_SECRET_KEY is required when Stripe is selected in production")
            if not self.stripe_webhook_secret:
                problems.append("PAYJENT_STRIPE_WEBHOOK_SECRET is required when Stripe is selected in production")
        if problems:
            raise RuntimeError("Payjent production guardrails failed: " + "; ".join(problems))

    def ensure_db_reset_allowed(self) -> None:
        if self.is_production and not self.allow_unsafe_db_reset:
            raise RuntimeError(
                "Refusing to reset the database in production. Set PAYJENT_ALLOW_UNSAFE_DB_RESET=true "
                "only for an intentional, pre-live disposable reset."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
