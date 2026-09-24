"""Application settings loaded from environment variables (and `.env` in local dev)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/wallet_risk"
    etherscan_api_key: str | None = None
    # Left unset until verified against Etherscan's current docs in Phase 1.
    etherscan_rate_limit_per_sec: float | None = None
    balances_api_key: str | None = None
    price_api_key: str | None = None
    cache_ttl_seconds: int | None = None
    log_level: str = "INFO"
    # Comma-separated list, e.g. "http://localhost:5173,https://example.com".
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
