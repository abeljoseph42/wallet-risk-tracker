"""Cache-aware transaction fetch layer backed by Postgres.

A lookup for (address, endpoint) is served from Postgres when `fetch_log` says the
history was fetched within the TTL. Otherwise it fetches from Etherscan, starting at
the last block already cached (incremental refresh), upserts the transactions, and
advances `fetch_log`. Every lookup writes one `api_metrics` row.
"""

import datetime
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.etherscan import EtherscanTransaction, FetchedTransactions
from app.core.addresses import normalize_address
from app.db.models import ApiMetric, FetchLog, Transaction

ENDPOINT_TXLIST = "txlist"

# asyncpg caps a statement at 32,767 bind parameters; 8 columns x 1,000 rows stays well under.
_UPSERT_CHUNK = 1_000


class TransactionSource(Protocol):
    async def get_normal_transactions(
        self, address: str, *, startblock: int = ...
    ) -> FetchedTransactions: ...


@dataclass(frozen=True)
class CacheLookup:
    transactions: list[Transaction]
    cache_hit: bool
    upstream_calls: int


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class TransactionCache:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        source: TransactionSource,
        *,
        ttl_seconds: int,
        now: Callable[[], datetime.datetime] = _utcnow,
    ) -> None:
        self._sessions = sessions
        self._source = source
        self._ttl = datetime.timedelta(seconds=ttl_seconds)
        self._now = now

    async def get_normal_transactions(self, address: str) -> CacheLookup:
        address = normalize_address(address)
        started = time.perf_counter()

        async with self._sessions() as session:
            log = await session.get(FetchLog, (address, ENDPOINT_TXLIST))
            now = self._now()
            cache_hit = log is not None and log.complete and now - log.last_fetched_at < self._ttl
            upstream_calls = 0

            if not cache_hit:
                # Re-read the last cached block rather than last_block + 1: upserts are
                # idempotent, and it guards against a block that was only partly indexed.
                startblock = log.last_block if log is not None else 0
                fetched = await self._source.get_normal_transactions(address, startblock=startblock)
                upstream_calls = fetched.request_count
                await _upsert_transactions(session, fetched.transactions)
                last_block = max(
                    (tx.block_number for tx in fetched.transactions), default=startblock
                )
                await _record_fetch(session, address, now, last_block)

            transactions = await _load_transactions(session, address)
            session.add(
                ApiMetric(
                    ts=now,
                    endpoint=ENDPOINT_TXLIST,
                    cache_hit=cache_hit,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    upstream_calls=upstream_calls,
                )
            )
            await session.commit()

        return CacheLookup(transactions, cache_hit, upstream_calls)


async def _upsert_transactions(
    session: AsyncSession, transactions: Sequence[EtherscanTransaction]
) -> None:
    rows = [
        {
            "hash": tx.hash.lower(),
            "from_addr": tx.from_address.lower(),
            "to_addr": (tx.to_address or tx.contract_address).lower(),
            "value_wei": tx.value_wei,
            "block_number": tx.block_number,
            "timestamp": datetime.datetime.fromtimestamp(tx.timestamp, datetime.UTC),
            "is_error": tx.is_error,
            "source_endpoint": ENDPOINT_TXLIST,
        }
        for tx in transactions
    ]
    for i in range(0, len(rows), _UPSERT_CHUNK):
        stmt = insert(Transaction).values(rows[i : i + _UPSERT_CHUNK])
        await session.execute(stmt.on_conflict_do_nothing(index_elements=["hash"]))


async def _record_fetch(
    session: AsyncSession, address: str, fetched_at: datetime.datetime, last_block: int
) -> None:
    stmt = insert(FetchLog).values(
        address=address,
        endpoint=ENDPOINT_TXLIST,
        last_fetched_at=fetched_at,
        last_block=last_block,
        complete=True,
    )
    # Upsert so two concurrent refreshes of one address don't collide on the primary key.
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["address", "endpoint"],
            set_={
                "last_fetched_at": stmt.excluded.last_fetched_at,
                "last_block": func.greatest(FetchLog.last_block, stmt.excluded.last_block),
                "complete": True,
            },
        )
    )
    # The upsert bypasses the ORM, so drop any FetchLog already loaded into this session.
    session.expire_all()


async def _load_transactions(session: AsyncSession, address: str) -> list[Transaction]:
    result = await session.scalars(
        select(Transaction)
        .where(
            Transaction.source_endpoint == ENDPOINT_TXLIST,
            or_(Transaction.from_addr == address, Transaction.to_addr == address),
        )
        .order_by(Transaction.block_number, Transaction.hash)
    )
    return list(result)


@dataclass(frozen=True)
class CacheMetrics:
    lookups: int
    hits: int
    upstream_calls: int

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    @property
    def calls_saved(self) -> int:
        """Lower bound: each cache hit avoided at least one Etherscan request.

        A cold fetch of a large history costs one request per 1,000 transactions, so the
        true saving is usually higher; we report the bound we can defend.
        """
        return self.hits

    @property
    def call_reduction(self) -> float:
        """Share of would-be Etherscan requests avoided (using the calls_saved lower bound)."""
        would_be = self.calls_saved + self.upstream_calls
        return self.calls_saved / would_be if would_be else 0.0


async def summarize_metrics(session: AsyncSession, endpoint: str = ENDPOINT_TXLIST) -> CacheMetrics:
    row = (
        await session.execute(
            select(
                func.count(ApiMetric.id),
                func.count(ApiMetric.id).filter(ApiMetric.cache_hit),
                func.coalesce(func.sum(ApiMetric.upstream_calls), 0),
            ).where(ApiMetric.endpoint == endpoint)
        )
    ).one()
    return CacheMetrics(lookups=row[0], hits=row[1], upstream_calls=int(row[2]))
