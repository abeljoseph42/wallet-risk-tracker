"""Application settings (environment / `.env`) and tunable parameters (`config/*.yaml`)."""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
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


SCORING_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "scoring.yaml"


class GraphParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_hops: int = Field(ge=1, le=6)
    max_expanded_nodes: int = Field(ge=1)
    max_neighbors_per_node: int = Field(ge=1)
    max_nodes: int = Field(ge=1)
    max_records_per_node: int = Field(ge=1)
    max_records_target: int = Field(ge=1)
    degree_threshold: int = Field(ge=1)
    skip_failed: bool = True
    skip_zero_value: bool = True


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def load_graph_params(path: Path = SCORING_CONFIG_PATH) -> GraphParams:
    data = _load_yaml(path)
    return GraphParams(max_hops=data["max_hops"], **data["graph"])
