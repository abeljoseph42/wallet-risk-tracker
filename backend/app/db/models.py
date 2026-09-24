"""ORM models for cached Etherscan data and call metrics.

`address_labels` and `score_runs` arrive in later phases; only the tables
Phase 1 (Etherscan client + cache layer) needs are defined here.
"""

import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Transaction(Base):
    """A single normal (external) ETH transfer, as returned by Etherscan's txlist."""

    __tablename__ = "transactions"
    __table_args__ = (
        Index("ix_transactions_from_addr", "from_addr"),
        Index("ix_transactions_to_addr", "to_addr"),
    )

    hash: Mapped[str] = mapped_column(String(66), primary_key=True)
    from_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    to_addr: Mapped[str] = mapped_column(String(42), nullable=False)
    # NUMERIC(78,0) holds any uint256 exactly; read back as Decimal, never float.
    value_wei: Mapped[Decimal] = mapped_column(Numeric(precision=78, scale=0), nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_endpoint: Mapped[str] = mapped_column(String(64), nullable=False)


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
    complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


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
