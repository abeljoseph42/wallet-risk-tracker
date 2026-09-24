"""Tests for TransactionCache against a real Postgres and a fake Etherscan source."""

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clients.etherscan import EtherscanTransaction, FetchedTransactions
from app.core.addresses import InvalidAddressError
from app.db.models import ApiMetric, FetchLog
from app.services.cache import ENDPOINT_TXLIST, TransactionCache, summarize_metrics

WALLET = "0x" + "a" * 40
OTHER = "0x" + "b" * 40
TTL_SECONDS = 3600


def _tx(n: int, block: int, *, to: str = OTHER, contract: str = "") -> EtherscanTransaction:
    return EtherscanTransaction(
        hash=f"0x{n:064x}",
        block_number=block,
        timestamp=1_700_000_000 + block,
        from_address=WALLET,
        to_address=to,
        contract_address=contract,
        value_wei=10**18 * n,
        is_error=False,
        gas_used=21_000,
    )


class FakeEtherscan:
    """Serves a fixed history, honouring startblock, and records every call."""

    def __init__(self, history: list[EtherscanTransaction]) -> None:
        self.history = history
        self.calls: list[tuple[str, int]] = []

    async def get_normal_transactions(
        self, address: str, *, startblock: int = 0
    ) -> FetchedTransactions:
        self.calls.append((address, startblock))
        txs = [t for t in self.history if t.block_number >= startblock]
        return FetchedTransactions(txs, request_count=1)


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

    result = await cache.get_normal_transactions(WALLET)

    assert not result.cache_hit
    assert result.upstream_calls == 1
    assert source.calls == [(WALLET, 0)]
    assert [t.block_number for t in result.transactions] == [100, 105]
    async with sessions() as session:
        log = await session.get(FetchLog, (WALLET, ENDPOINT_TXLIST))
    assert log is not None
    assert log.last_block == 105
    assert log.complete


async def test_second_lookup_within_ttl_makes_zero_etherscan_calls(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(1, 100), _tx(2, 105)])
    clock = FakeClock()
    cache = _cache(sessions, source, clock)
    first = await cache.get_normal_transactions(WALLET)

    clock.now += datetime.timedelta(seconds=TTL_SECONDS - 1)
    second = await cache.get_normal_transactions(WALLET)

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
    await cache.get_normal_transactions(WALLET)

    source.history.append(_tx(3, 200))
    clock.now += datetime.timedelta(seconds=TTL_SECONDS + 1)
    result = await cache.get_normal_transactions(WALLET)

    assert source.calls == [(WALLET, 0), (WALLET, 105)]
    assert not result.cache_hit
    # Block 105 is re-read on purpose; the upsert must not duplicate it.
    assert [t.block_number for t in result.transactions] == [100, 105, 200]
    async with sessions() as session:
        log = await session.get(FetchLog, (WALLET, ENDPOINT_TXLIST))
    assert log is not None
    assert log.last_block == 200


async def test_empty_history_is_cached_too(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([])
    cache = _cache(sessions, source, FakeClock())

    first = await cache.get_normal_transactions(WALLET)
    second = await cache.get_normal_transactions(WALLET)

    assert first.transactions == second.transactions == []
    assert second.cache_hit
    assert len(source.calls) == 1


async def test_mixed_case_address_is_normalized_to_one_cache_entry(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([_tx(1, 100)])
    cache = _cache(sessions, source, FakeClock())
    mixed = "0x" + "Aa" * 20

    await cache.get_normal_transactions(mixed)
    second = await cache.get_normal_transactions(mixed.lower())

    assert source.calls == [("0x" + "aa" * 20, 0)]
    assert second.cache_hit


async def test_invalid_address_is_rejected_before_any_fetch(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    source = FakeEtherscan([])
    cache = _cache(sessions, source, FakeClock())

    with pytest.raises(InvalidAddressError):
        await cache.get_normal_transactions("0x123")

    assert source.calls == []


async def test_contract_creation_is_stored_against_the_new_contract(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    contract = "0x" + "c" * 40
    source = FakeEtherscan([_tx(1, 100, to="", contract=contract)])
    cache = _cache(sessions, source, FakeClock())

    result = await cache.get_normal_transactions(WALLET)

    assert result.transactions[0].to_addr == contract


async def test_wei_values_round_trip_without_precision_loss(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    huge = 2**256 - 1
    tx = _tx(1, 100).model_copy(update={"value_wei": huge})
    cache = _cache(sessions, FakeEtherscan([tx]), FakeClock())

    result = await cache.get_normal_transactions(WALLET)

    assert int(result.transactions[0].value_wei) == huge


async def test_every_lookup_is_recorded_and_summarized(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    cache = _cache(sessions, FakeEtherscan([_tx(1, 100)]), FakeClock())
    for _ in range(4):
        await cache.get_normal_transactions(WALLET)

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
    await cache.get_normal_transactions(WALLET)
    await cache.get_normal_transactions(WALLET)

    async with sessions() as session:
        metrics = await summarize_metrics(session)

    # 2,500 cached txs = 3 txlist pages a cold refetch would need.
    assert metrics.calls_saved == 3
    assert metrics.upstream_calls == 1
    assert metrics.call_reduction == 0.75
