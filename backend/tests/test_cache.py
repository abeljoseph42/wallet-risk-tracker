"""Tests for TransactionCache against a real Postgres and a fake Etherscan source."""

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.etherscan import Endpoint, FetchedTransfers, Transfer
from app.core.addresses import InvalidAddressError
from app.db.models import ApiMetric, FetchLog
from app.services.cache import TransactionCache, summarize_metrics

WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
TOKEN = "0x" + "c" * 40
TTL_SECONDS = 3600
TXLIST = Endpoint.NORMAL.value


def _tx(
    n: int,
    block: int,
    *,
    to: str = OTHER,
    endpoint: Endpoint = Endpoint.NORMAL,
    sub_key: str = "",
    value: int | None = None,
) -> Transfer:
    return Transfer(
        endpoint=endpoint,
        hash=f"0x{n:064x}",
        sub_key=sub_key,
        block_number=block,
        timestamp=1_700_000_000 + block,
        from_address=WALLET,
        to_address=to,
        value=10**18 * n if value is None else value,
        token_address=TOKEN if endpoint is Endpoint.TOKEN else None,
        token_symbol="TKN" if endpoint is Endpoint.TOKEN else None,
        token_decimals=18 if endpoint is Endpoint.TOKEN else None,
        is_error=False,
    )


class FakeEtherscan:
    """Serves fixed histories per endpoint, honouring startblock and max_records."""

    def __init__(self, history: list[Transfer]) -> None:
        self.history = history
        self.calls: list[tuple[str, int]] = []
        self.calls_by_endpoint: list[tuple[Endpoint, int, int | None]] = []

    async def fetch_transfers(
        self,
        endpoint: Endpoint,
        address: str,
        *,
        startblock: int = 0,
        max_records: int | None = None,
    ) -> FetchedTransfers:
        self.calls.append((address, startblock))
        self.calls_by_endpoint.append((endpoint, startblock, max_records))
        txs = [t for t in self.history if t.endpoint is endpoint and t.block_number >= startblock]
        if max_records is not None and len(txs) > max_records:
            return FetchedTransfers(txs[:max_records], request_count=1, truncated=True)
        return FetchedTransfers(txs, request_count=1)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime.datetime(2026, 9, 24, 12, 0, tzinfo=datetime.UTC)

    def __call__(self) -> datetime.datetime:
        return self.now


def _cache(
    sessions: async_sessionmaker[AsyncSession], source: FakeEtherscan, clock: FakeClock
) -> TransactionCache:
    return TransactionCache(sessions, source, ttl_seconds=TTL_SECONDS, now=clock)


async def test_first_lookup_is_a_miss_that_fetches_and_stores(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(1, 100), _tx(2, 105)])
    cache = _cache(sessions, source, FakeClock())

    result = await cache.lookup(WALLET)

    assert not result.cache_hit
    assert result.upstream_calls == 1
    assert source.calls == [(WALLET, 0)]
    assert [t.block_number for t in result.transactions] == [100, 105]
    async with sessions() as session:
        log = await session.get(FetchLog, (WALLET, TXLIST))
    assert log is not None
    assert log.last_block == 105
    assert log.complete


async def test_second_lookup_within_ttl_makes_zero_etherscan_calls(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(1, 100), _tx(2, 105)])
    clock = FakeClock()
    cache = _cache(sessions, source, clock)
    first = await cache.lookup(WALLET)

    clock.now += datetime.timedelta(seconds=TTL_SECONDS - 1)
    second = await cache.lookup(WALLET)

    assert len(source.calls) == 1
    assert second.cache_hit
    assert second.upstream_calls == 0
    assert [t.hash for t in second.transactions] == [t.hash for t in first.transactions]


async def test_stale_lookup_refreshes_incrementally_from_last_block(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(1, 100), _tx(2, 105)])
    clock = FakeClock()
    cache = _cache(sessions, source, clock)
    await cache.lookup(WALLET)

    source.history.append(_tx(3, 200))
    clock.now += datetime.timedelta(seconds=TTL_SECONDS + 1)
    result = await cache.lookup(WALLET)

    assert source.calls == [(WALLET, 0), (WALLET, 105)]
    assert not result.cache_hit
    # Block 105 is re-read on purpose; the upsert must not duplicate it.
    assert [t.block_number for t in result.transactions] == [100, 105, 200]
    async with sessions() as session:
        log = await session.get(FetchLog, (WALLET, TXLIST))
    assert log is not None
    assert log.last_block == 200


async def test_empty_history_is_cached_too(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([])
    cache = _cache(sessions, source, FakeClock())

    first = await cache.lookup(WALLET)
    second = await cache.lookup(WALLET)

    assert first.transactions == second.transactions == []
    assert second.cache_hit
    assert len(source.calls) == 1


async def test_mixed_case_address_is_normalized_to_one_cache_entry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(1, 100)])
    cache = _cache(sessions, source, FakeClock())
    mixed = "0x" + "Aa" * 20

    await cache.lookup(mixed)
    second = await cache.lookup(mixed.lower())

    assert source.calls == [("0x" + "aa" * 20, 0)]
    assert second.cache_hit


async def test_invalid_address_is_rejected_before_any_fetch(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([])
    cache = _cache(sessions, source, FakeClock())

    with pytest.raises(InvalidAddressError):
        await cache.lookup("0x123")

    assert source.calls == []


async def test_wei_values_round_trip_without_precision_loss(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    huge = 2**256 - 1
    tx = _tx(1, 100, value=huge)
    cache = _cache(sessions, FakeEtherscan([tx]), FakeClock())

    result = await cache.lookup(WALLET)

    assert int(result.transactions[0].value_raw) == huge


async def test_every_lookup_is_recorded_and_summarized(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cache = _cache(sessions, FakeEtherscan([_tx(1, 100)]), FakeClock())
    for _ in range(4):
        await cache.lookup(WALLET)

    async with sessions() as session:
        rows = (await session.scalars(select(ApiMetric).order_by(ApiMetric.id))).all()
        metrics = await summarize_metrics(session)

    assert [r.cache_hit for r in rows] == [False, True, True, True]
    assert [r.upstream_calls for r in rows] == [1, 0, 0, 0]
    assert metrics.lookups == 4
    assert metrics.hits == 3
    assert metrics.upstream_calls == 1
    assert metrics.hit_rate == 0.75
    assert metrics.calls_saved == 3
    assert metrics.call_reduction == 0.75


async def test_hit_on_large_history_counts_one_saved_call_per_page(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    history = [_tx(n, 100 + n) for n in range(1, 2_501)]
    cache = _cache(sessions, FakeEtherscan(history), FakeClock())
    await cache.lookup(WALLET)
    await cache.lookup(WALLET)

    async with sessions() as session:
        metrics = await summarize_metrics(session)

    # 2,500 cached txs = 3 txlist pages a cold refetch would need.
    assert metrics.calls_saved == 3
    assert metrics.upstream_calls == 1
    assert metrics.call_reduction == 0.75


async def test_capped_read_is_stored_as_truncated_and_reused_under_the_same_cap(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(n, 100 + n) for n in range(1, 6)])
    cache = _cache(sessions, source, FakeClock())

    first = await cache.lookup(WALLET, max_records=3)
    second = await cache.lookup(WALLET, max_records=3)

    assert first.truncated and not first.cache_hit
    assert second.truncated and second.cache_hit
    assert len(source.calls) == 1
    async with sessions() as session:
        log = await session.get(FetchLog, (WALLET, TXLIST))
    assert log is not None
    assert not log.complete
    assert log.record_count == 3


async def test_higher_cap_resumes_from_the_last_cached_block(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(n, 100 + n) for n in range(1, 6)])
    cache = _cache(sessions, source, FakeClock())
    await cache.lookup(WALLET, max_records=3)

    full = await cache.lookup(WALLET)

    # Resumes at block 103 (the last cached one) with no cap, and completes the history.
    assert source.calls_by_endpoint[1] == (Endpoint.NORMAL, 103, None)
    assert not full.truncated
    assert [t.block_number for t in full.transactions] == [101, 102, 103, 104, 105]
    async with sessions() as session:
        log = await session.get(FetchLog, (WALLET, TXLIST))
    assert log is not None
    assert log.complete


async def test_complete_history_serves_any_cap(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(1, 101), _tx(2, 102)])
    cache = _cache(sessions, source, FakeClock())
    await cache.lookup(WALLET)

    capped = await cache.lookup(WALLET, max_records=1)

    assert capped.cache_hit
    assert not capped.truncated
    assert len(source.calls) == 1


async def test_lookup_all_keeps_endpoints_separate(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    same_hash = [
        _tx(1, 100),
        _tx(1, 100, endpoint=Endpoint.INTERNAL, sub_key="0_1"),
        _tx(1, 100, endpoint=Endpoint.INTERNAL, sub_key="0_2"),
        _tx(1, 100, endpoint=Endpoint.TOKEN, sub_key=f"{TOKEN}:{WALLET}:{OTHER}:5", value=5),
    ]
    cache = _cache(sessions, FakeEtherscan(same_hash), FakeClock())

    results = await cache.lookup_all(WALLET)

    assert {e: len(r.transactions) for e, r in results.items()} == {
        Endpoint.NORMAL: 1,
        Endpoint.INTERNAL: 2,
        Endpoint.TOKEN: 1,
    }
    token = results[Endpoint.TOKEN].transactions[0]
    assert token.token_address == TOKEN
    assert token.token_symbol == "TKN"
    assert int(token.value_raw) == 5
    async with sessions() as session:
        metrics = await summarize_metrics(session)
        normal_only = await summarize_metrics(session, TXLIST)
    assert metrics.lookups == 3
    assert normal_only.lookups == 1
