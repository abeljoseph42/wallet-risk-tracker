"""Cache-aware transfer fetch layer backed by Postgres.

A lookup for (address, endpoint) is served from Postgres when `fetch_log` says the
history was fetched within the TTL. Otherwise it fetches from Etherscan, starting at
the last block already cached (incremental refresh), upserts the transfers, and
advances `fetch_log`. Every lookup writes one `api_metrics` row.

A lookup can pass `max_records` to bound its API cost. A capped read that stops early
is stored with `complete=False`; it is served as-is to later lookups whose cap it
already satisfies, and resumed from `last_block` by lookups that need more.
"""

import asyncio
import datetime
import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.etherscan import TXLIST_PAGE_SIZE, Endpoint, FetchedTransfers, Transfer
from app.core.addresses import normalize_address
from app.db.models import ApiMetric, FetchLog, Transaction

# asyncpg caps a statement at 32,767 bind parameters; 12 columns x 1,000 rows stays under.
_UPSERT_CHUNK = 1_000


class TransferSource(Protocol):
    async def fetch_transfers(
        self,
        endpoint: Endpoint,
        address: str,
        *,
        startblock: int = ...,
        max_records: int | None = ...,
    ) -> FetchedTransfers: ...


@dataclass(frozen=True)
class CacheLookup:
    transactions: list[Transaction]
    cache_hit: bool
    upstream_calls: int
    # The cached history stops before the chain head because a record cap was hit.
    truncated: bool = False


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


class TransactionCache:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        source: TransferSource,
        *,
        ttl_seconds: int,
        now: Callable[[], datetime.datetime] = _utcnow,
    ) -> None:
        self._sessions = sessions
        self._source = source
        self._ttl = datetime.timedelta(seconds=ttl_seconds)
        self._now = now

    async def lookup(
        self,
        address: str,
        endpoint: Endpoint = Endpoint.NORMAL,
        *,
        max_records: int | None = None,
    ) -> CacheLookup:
        address = normalize_address(address)
        started = time.perf_counter()

        async with self._sessions() as session:
            log = await session.get(FetchLog, (address, endpoint.value))
            now = self._now()
            cache_hit = log is not None and _serves(
                log, now - log.last_fetched_at, self._ttl, max_records
            )
            upstream_calls = 0
            truncated = log is not None and not log.complete

            if not cache_hit:
                # Re-read the last cached block rather than last_block + 1: upserts are
                # idempotent, and a capped or windowed read may have stopped mid-block.
                startblock = log.last_block if log is not None else 0
                already = log.record_count if log is not None else 0
                remaining = None if max_records is None else max(1, max_records - already)
                fetched = await self._source.fetch_transfers(
                    endpoint, address, startblock=startblock, max_records=remaining
                )
                upstream_calls = fetched.request_count
                truncated = fetched.truncated
                await _upsert_transfers(session, fetched.transfers)
                last_block = max((t.block_number for t in fetched.transfers), default=startblock)
                await _record_fetch(
                    session,
                    address,
                    endpoint,
                    now,
                    last_block=last_block,
                    complete=not fetched.truncated,
                    record_count=already + len(fetched.transfers),
                )

            transactions = await _load_transactions(session, address, endpoint)
            calls_avoided = (
                max(1, math.ceil(len(transactions) / TXLIST_PAGE_SIZE)) if cache_hit else 0
            )
            session.add(
                ApiMetric(
                    ts=now,
                    endpoint=endpoint.value,
                    cache_hit=cache_hit,
                    latency_ms=round((time.perf_counter() - started) * 1000),
                    upstream_calls=upstream_calls,
                    calls_avoided=calls_avoided,
                )
            )
            await session.commit()

        return CacheLookup(transactions, cache_hit, upstream_calls, truncated)

    async def lookup_all(
        self, address: str, *, max_records: int | None = None
    ) -> dict[Endpoint, CacheLookup]:
        """Normal, internal and token transfers for `address` (one lookup per endpoint)."""
        results = await asyncio.gather(
            *(self.lookup(address, endpoint, max_records=max_records) for endpoint in Endpoint)
        )
        return dict(zip(Endpoint, results, strict=True))


def _serves(
    log: FetchLog, age: datetime.timedelta, ttl: datetime.timedelta, max_records: int | None
) -> bool:
    if age >= ttl:
        return False
    if log.complete:
        return True
    # A capped read satisfies any lookup whose own cap it already met.
    return max_records is not None and log.record_count >= max_records


async def _upsert_transfers(session: AsyncSession, transfers: Sequence[Transfer]) -> None:
    rows = [
        {
            "source_endpoint": t.endpoint.value,
            "hash": t.hash,
            "sub_key": t.sub_key,
            "from_addr": t.from_address,
            "to_addr": t.to_address,
            "value_raw": t.value,
            "token_address": t.token_address,
            "token_symbol": t.token_symbol,
            "token_decimals": t.token_decimals,
            "block_number": t.block_number,
            "timestamp": datetime.datetime.fromtimestamp(t.timestamp, datetime.UTC),
            "is_error": t.is_error,
        }
        for t in transfers
    ]
    for i in range(0, len(rows), _UPSERT_CHUNK):
        stmt = insert(Transaction).values(rows[i : i + _UPSERT_CHUNK])
        await session.execute(
            stmt.on_conflict_do_nothing(index_elements=["source_endpoint", "hash", "sub_key"])
        )


async def _record_fetch(
    session: AsyncSession,
    address: str,
    endpoint: Endpoint,
    fetched_at: datetime.datetime,
    *,
    last_block: int,
    complete: bool,
    record_count: int,
) -> None:
    stmt = insert(FetchLog).values(
        address=address,
        endpoint=endpoint.value,
        last_fetched_at=fetched_at,
        last_block=last_block,
        complete=complete,
        record_count=record_count,
    )
    # Upsert so two concurrent refreshes of one address don't collide on the primary key.
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["address", "endpoint"],
            set_={
                "last_fetched_at": stmt.excluded.last_fetched_at,
                "last_block": func.greatest(FetchLog.last_block, stmt.excluded.last_block),
                "complete": stmt.excluded.complete,
                "record_count": stmt.excluded.record_count,
            },
        )
    )
    # The upsert bypasses the ORM, so drop any FetchLog already loaded into this session.
    session.expire_all()


async def _load_transactions(
    session: AsyncSession, address: str, endpoint: Endpoint
) -> list[Transaction]:
    result = await session.scalars(
        select(Transaction)
        .where(
            Transaction.source_endpoint == endpoint.value,
            or_(Transaction.from_addr == address, Transaction.to_addr == address),
        )
        .order_by(Transaction.block_number, Transaction.hash, Transaction.sub_key)
    )
    return list(result)


@dataclass(frozen=True)
class CacheMetrics:
    lookups: int
    hits: int
    upstream_calls: int
    # Lower bound: pages a cold refetch would need, excluding retries it might also hit.
    calls_saved: int

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    @property
    def call_reduction(self) -> float:
        """Share of would-be Etherscan requests the cache avoided."""
        would_be = self.calls_saved + self.upstream_calls
        return self.calls_saved / would_be if would_be else 0.0


async def summarize_metrics(session: AsyncSession, endpoint: str | None = None) -> CacheMetrics:
    """Totals across all endpoints, or for one endpoint."""
    stmt = select(
        func.count(ApiMetric.id),
        func.count(ApiMetric.id).filter(ApiMetric.cache_hit),
        func.coalesce(func.sum(ApiMetric.upstream_calls), 0),
        func.coalesce(func.sum(ApiMetric.calls_avoided), 0),
    )
    if endpoint is not None:
        stmt = stmt.where(ApiMetric.endpoint == endpoint)
    row = (await session.execute(stmt)).one()
    return CacheMetrics(
        lookups=row[0], hits=row[1], upstream_calls=int(row[2]), calls_saved=int(row[3])
    )
