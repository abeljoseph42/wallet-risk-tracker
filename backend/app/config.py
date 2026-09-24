"""Application settings loaded from environment variables (and `.env` in local dev)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    # Host port 5433 avoids clashing with a natively installed Postgres on 5432.
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5433/wallet_risk"
    etherscan_api_key: str | None = None
    # Free tier per docs.etherscan.io/rate-limits (checked 2026-09): 3 calls/s, 100k/day.
    etherscan_rate_limit_per_sec: float = 3.0
    balances_api_key: str | None = None
    price_api_key: str | None = None
    # How long a cached transaction history is served before an incremental refresh.
    cache_ttl_seconds: int = 3600
    log_level: str = "INFO"
    # Comma-separated list, e.g. "http://localhost:5173,https://example.com".
    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
