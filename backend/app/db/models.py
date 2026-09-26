"""ORM models for cached Etherscan data, call metrics, and address labels."""

import datetime
import uuid
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Transaction(Base):
    """One value transfer: a normal tx, an internal call, or an ERC-20 transfer.

    Keyed by (source_endpoint, hash, sub_key) because one transaction hash can carry
    several internal calls and token transfers. See `clients.etherscan.Transfer.sub_key`.
    """

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_from_addr", "from_addr"),
        Index("ix_transactions_to_addr", "to_addr"),
    )

    source_endpoint: Mapped[str] = mapped_column(String(64), primary_key=True)
    hash: Mapped[str] = mapped_column(String(66), primary_key=True)
    sub_key: Mapped[str] = mapped_column(String(255), primary_key=True, server_default="")
    from_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    to_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    # Base units of the asset (wei for ETH). NUMERIC(78,0) holds any uint256 exactly;
    # read back as Decimal, never float.
    value_raw: Mapped[Decimal] = mapped_column(Numeric(precision=78, scale=0), nullable=False)
    # NULL for ETH (normal and internal transfers); the token contract otherwise.
    token_address: Mapped[str | None] = mapped_column(String(42))
    token_symbol: Mapped[str | None] = mapped_column(String)
    token_decimals: Mapped[int | None] = mapped_column(Integer)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class FetchLog(Base):
    """Tracks what has already been fetched for (address, endpoint), for incremental refresh."""

    __tablename__ = "fetch_log"
    __table_args__ = (Index("ix_fetch_log_address_endpoint", "address", "endpoint"),)

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_fetched_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_block: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    # True: read through the chain head as of last_fetched_at. False: a record cap stopped
    # the read at last_block; a later lookup with a higher cap resumes from there.
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Records fetched so far for this (address, endpoint), across incremental reads.
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


class ApiMetric(Base):
    """One row per cache lookup, used to compute cache-hit rate and calls saved."""

    __tablename__ = "api_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # HTTP requests sent to Etherscan for this lookup, retries included (0 on a cache hit).
    upstream_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    # On a hit, the requests a cold fetch of this history would need: one per txlist page.
    calls_avoided: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")


LABEL_TYPES = ("sanctioned", "malicious", "exchange", "mixer")


class AddressLabel(Base):
    """Ground-truth and exchange labels. One address can carry several label types."""

    __tablename__ = "address_labels"
    __table_args__ = (
        CheckConstraint(
            "label_type IN ('sanctioned', 'malicious', 'exchange', 'mixer')",
            name="ck_address_labels_label_type",
        ),
        CheckConstraint(
            "severity IS NULL OR (severity >= 0 AND severity <= 1)",
            name="ck_address_labels_severity",
        ),
        Index("ix_address_labels_source", "source"),
    )

    address: Mapped[str] = mapped_column(String(42), primary_key=True)
    label_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    # Stable dataset id; re-ingesting a source replaces exactly that source's rows.
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str | None] = mapped_column(String(256))
    # Per-address override; NULL means use the label type's default in scoring.yaml.
    severity: Mapped[float | None] = mapped_column(Float)
    added_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


SCORE_RUN_STATUSES = ("pending", "running", "done", "failed")


class ScoreRun(Base):
    """One scoring request: the job's state while it runs, then its full result."""

    __tablename__ = "score_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'done', 'failed')", name="ck_score_runs_status"
        ),
        # Serves "latest run for this address and params" (reuse) and history listings.
        Index("ix_score_runs_address_params_created", "address", "params_hash", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    address: Mapped[str] = mapped_column(String(42), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    params_hash: Mapped[str] = mapped_column(String(12), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    bucket: Mapped[str | None] = mapped_column(String(16))
    # Flagged-address breakdown, flagged subgraph (nodes/edges) and traversal stats.
    breakdown_json: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB)
    graph_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    stats_json: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(32))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
