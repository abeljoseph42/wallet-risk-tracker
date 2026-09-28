"""Request and response models for the scoring API (also the OpenAPI schema)."""

import datetime
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.addresses import InvalidAddressError, normalize_address
from app.db.models import ScoreRun

_WEI = Field(description="Amount in wei, as a decimal string (can exceed 2^53).")


class ScoreRequest(BaseModel):
    address: str = Field(
        description="Ethereum address. Lowercase, uppercase, or a valid EIP-55 checksum.",
        examples=["0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"],
    )

    @field_validator("address")
    @classmethod
    def _valid_address(cls, value: str) -> str:
        try:
            return normalize_address(value)
        except InvalidAddressError as exc:
            raise ValueError(str(exc)) from exc


class Contribution(BaseModel):
    address: str
    name: str | None = Field(
        default=None, description='Label name, e.g. "Tornado.Cash: 10 ETH", if known.'
    )
    label: Literal["sanctioned", "malicious", "mixer"] = Field(
        description="The address's most severe risk label."
    )
    labels: list[str]
    severity: float
    hops: int = Field(description="Hops from the target on the chosen path (0 = the target).")
    path: list[str] = Field(description="Addresses from the target to the flagged address.")
    via: list[str] = Field(description="Exchanges or high-degree hubs on the path.")
    bottleneck_eq_wei: str = _WEI
    flow_share: float = Field(description="Bottleneck value / target's total volume.")
    flow_factor: float
    hop_weight: float
    discount: float
    contribution: float = Field(description="severity * hop_weight * flow_factor * discount")


class GraphNode(BaseModel):
    address: str
    role: Literal["target", "flagged", "path"]
    labels: list[str]
    hop: int | None
    is_hub: bool


class GraphEdge(BaseModel):
    source: str
    target: str
    tx_count: int
    token_transfer_count: int
    total_value_wei: str = Field(description="ETH moved on this edge, in wei (decimal string).")
    value_eq_wei: str = Field(
        description="ETH plus valued stablecoins, in ETH-equivalent wei (decimal string)."
    )
    last_seen: int | None = Field(description="Unix time of the latest transfer on this edge.")


class FlaggedGraph(BaseModel):
    """The paths in the breakdown only, not the whole traversed graph."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]


class TraversalStats(BaseModel):
    nodes: int
    edges: int
    expanded: int
    hubs: int
    lookups: int
    cache_hits: int
    api_calls: int = Field(description="Etherscan requests made for this score.")
    budget_exhausted: bool
    node_limit_reached: bool
    duration_ms: int


class ScoreResult(BaseModel):
    score: float = Field(ge=0, le=100)
    bucket: Literal["low", "medium", "high", "severe"]
    flagged: bool | None = Field(
        description=(
            "Whether the score reaches the tuned flag threshold (see docs/EVALUATION.md): "
            "the model's yes/no call on exposure. The bucket describes its strength. "
            "Null for runs made before this field existed."
        )
    )
    breakdown: list[Contribution]
    graph: FlaggedGraph
    stats: TraversalStats


class JobError(BaseModel):
    code: Literal[
        "upstream_rate_limited",
        "upstream_unavailable",
        "upstream_error",
        "not_configured",
        "timeout",
        "interrupted",
        "internal",
    ]
    message: str


class ScoreRunOut(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "description": "A scoring job. Poll until `status` is `done` or `failed`."
        }
    )

    id: uuid.UUID
    address: str
    status: Literal["pending", "running", "done", "failed"]
    params_hash: str = Field(description="Hash of the scoring parameters used.")
    created_at: datetime.datetime
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None
    result: ScoreResult | None = Field(description="Set when status is `done`.")
    error: JobError | None = Field(description="Set when status is `failed`.")

    @classmethod
    def from_run(cls, run: ScoreRun) -> "ScoreRunOut":
        result = None
        if run.status == "done":
            result = ScoreResult.model_validate(
                {
                    "score": run.score,
                    "bucket": run.bucket,
                    "flagged": run.flagged,
                    "breakdown": run.breakdown_json,
                    "graph": run.graph_json,
                    "stats": run.stats_json,
                }
            )
        error = None
        if run.status == "failed":
            error = JobError.model_validate(
                {"code": run.error_code, "message": run.error_message or ""}
            )
        return cls.model_validate(
            {
                "id": run.id,
                "address": run.address,
                "status": run.status,
                "params_hash": run.params_hash,
                "created_at": run.created_at,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "result": result,
                "error": error,
            }
        )


class ErrorDetail(BaseModel):
    detail: str


PortfolioWarning = Literal[
    "eth_balance_unavailable",
    "eth_balance_not_configured",
    "token_balances_unavailable",
    "token_balances_not_configured",
    "prices_unavailable",
]


class HoldingOut(BaseModel):
    token_address: str | None = Field(description="Token contract; null for ETH.")
    symbol: str
    decimals: int
    balance: str = Field(description="Balance in whole units, as a decimal string.")
    balance_raw: str = Field(description="Balance in base units (e.g. wei), decimal string.")
    price_usd: float | None
    value_usd: float | None
    price_confidence: float | None = Field(description="DefiLlama's 0-1 price confidence.")


class PortfolioOut(BaseModel):
    address: str
    eth: HoldingOut | None = Field(description="Null if the ETH balance is unavailable.")
    tokens: list[HoldingOut] = Field(description="Priced tokens, largest USD value first.")
    priced_token_count: int = Field(description="All priced tokens (the list may be capped).")
    unpriced_token_count: int = Field(
        description="Tokens with no reliable price, mostly airdropped spam; not listed."
    )
    total_usd: float | None = Field(description="ETH plus all priced tokens; null w/o prices.")
    warnings: list[PortfolioWarning] = Field(
        description="Data sources that were unavailable; the rest of the response is valid."
    )
    as_of: datetime.datetime
