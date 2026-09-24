"""ORM models for cached Etherscan data and call metrics.

`address_labels` and `score_runs` arrive in later phases; only the tables
Phase 1 (Etherscan client + cache layer) needs are defined here.
"""

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, Numeric, String
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
    value_wei: Mapped[int] = mapped_column(Numeric(precision=78, scale=0), nullable=False)
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
    """One row per outbound call, used to compute cache-hit rate and calls saved."""

    __tablename__ = "api_metrics"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latency_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
