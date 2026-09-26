"""Application settings (environment / `.env`) and tunable parameters (`config/*.yaml`)."""

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
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
    # Send at this fraction of the plan limit. Evenly spaced requests still reach Etherscan
    # bunched up by network jitter; at 100% a live graph build was throttled 5 times in 49.
    etherscan_rate_headroom: float = Field(default=0.8, gt=0, le=1)
    balances_api_key: str | None = None
    price_api_key: str | None = None
    # How long a cached transaction history is served before an incremental refresh.
    cache_ttl_seconds: int = 3600
    # Scoring jobs (Phase 5). They share one Etherscan rate limit, so few run at once.
    score_job_timeout_seconds: int = Field(default=300, gt=0)
    score_max_concurrent_jobs: int = Field(default=2, ge=1)
    # A finished score for the same address and params is returned instead of recomputed.
    score_reuse_seconds: int = Field(default=3600, ge=0)
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


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Severity(_Strict):
    sanctioned: float = Field(ge=0, le=1)
    malicious: float = Field(ge=0, le=1)
    mixer: float = Field(ge=0, le=1)


class ExchangeHandling(_Strict):
    enabled: bool
    mode: Literal["discount", "cut"]
    exchange_discount: float = Field(ge=0, le=1)


class Buckets(_Strict):
    low: float
    medium: float
    high: float
    severe: float

    @model_validator(mode="after")
    def _ascending(self) -> "Buckets":
        if not 0 <= self.low <= self.medium <= self.high <= self.severe <= 100:
            raise ValueError("bucket lower bounds must ascend within [0, 100]")
        return self


class Flow(_Strict):
    share_saturation: float = Field(gt=0, le=1)


class Stablecoin(_Strict):
    symbol: str
    decimals: int = Field(ge=0, le=36)


class Valuation(_Strict):
    usd_per_eth: float = Field(gt=0)
    stablecoins: dict[str, Stablecoin]

    @field_validator("stablecoins")
    @classmethod
    def _lowercase(cls, value: dict[str, Stablecoin]) -> dict[str, Stablecoin]:
        return {address.lower(): coin for address, coin in value.items()}


class ScoringParams(_Strict):
    max_hops: int = Field(ge=1, le=6)
    hop_decay: float = Field(gt=0, le=1)
    severity: Severity
    exchange_handling: ExchangeHandling
    buckets: Buckets
    flow: Flow
    valuation: Valuation
    graph: GraphParams

    @property
    def params_hash(self) -> str:
        """Short sha256 of the canonical parameters, reported with every score.

        Computed from the validated model (not the YAML text), so variants built in code,
        e.g. exchange handling switched off for an evaluation run, get their own hash.
        """
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def load_scoring_params(path: Path = SCORING_CONFIG_PATH) -> ScoringParams:
    data = _load_yaml(path)
    graph = GraphParams(max_hops=data["max_hops"], **data["graph"])
    return ScoringParams(**{**data, "graph": graph})
