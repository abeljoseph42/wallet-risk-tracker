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
