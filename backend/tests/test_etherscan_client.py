"""Tests for EtherscanClient against a mocked transport (no live API calls)."""

from collections.abc import Awaitable, Callable

import httpx
import pytest

from app.clients.etherscan import EtherscanClient, EtherscanError
from app.core.rate_limiter import TokenBucketRateLimiter


def _tx(hash_suffix: str, block_number: int) -> dict[str, object]:
    return {
        "hash": f"0x{hash_suffix}",
        "blockNumber": str(block_number),
        "timeStamp": "1700000000",
        "from": "0xfrom",
        "to": "0xto",
        "value": "1000000000000000000",
        "isError": "0",
        "gasUsed": "21000",
    }


def _ok(result: list[dict[str, object]]) -> dict[str, object]:
    return {"status": "1", "message": "OK", "result": result}


def _no_transactions() -> dict[str, object]:
    return {"status": "0", "message": "No transactions found", "result": []}


class _Handler:
    """Routes mock requests to canned JSON responses, recording calls made."""

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("no more canned responses")
        return httpx.Response(200, json=self._responses.pop(0))


def _client(
    handler: _Handler,
    *,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    max_retries: int = 5,
    max_record_window: int = 10_000,
) -> EtherscanClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    rate_limiter = TokenBucketRateLimiter(rate_per_sec=1000)
    return EtherscanClient(
        "fake-key",
        http_client=http_client,
        rate_limiter=rate_limiter,
        max_retries=max_retries,
        backoff_base_seconds=0.001,
        sleep=sleep,
        max_record_window=max_record_window,
    )


async def test_returns_empty_list_when_no_transactions_found() -> None:
    handler = _Handler([_no_transactions()])
    client = _client(handler)

    result = await client.get_normal_transactions("0xabc")

    assert result == []


async def test_parses_transactions_from_a_single_page() -> None:
    handler = _Handler([_ok([_tx("1", 100), _tx("2", 101)])])
    client = _client(handler)

    result = await client.get_normal_transactions("0xabc")

    assert [t.hash for t in result] == ["0x1", "0x2"]
    assert result[0].block_number == 100
    assert result[0].value_wei == 1_000_000_000_000_000_000


async def test_paginates_within_the_10000_record_window() -> None:
    full_page = _ok([_tx(str(i), 100 + i) for i in range(3)])
    partial_page = _ok([_tx("last", 200)])
    handler = _Handler([full_page, partial_page])
    client = _client(handler)

    result = await client.get_normal_transactions("0xabc", page_size=3)

    assert len(result) == 4
    assert [r.url.params["page"] for r in handler.requests] == ["1", "2"]


async def test_advances_startblock_past_the_record_window() -> None:
    # page_size=2, window=4: the client can fetch 2 full pages (4 records)
    # before it must re-window from the last block seen.
    window_one_pages = [
        _ok([_tx("w1-0", 100), _tx("w1-1", 101)]),
        _ok([_tx("w1-2", 102), _tx("w1-3", 103)]),
    ]
    window_two_page = _ok([_tx("w2-0", 200)])
    handler = _Handler([*window_one_pages, window_two_page])
    client = _client(handler, max_record_window=4)

    result = await client.get_normal_transactions("0xabc", page_size=2)

    assert len(result) == 5
    startblocks = [r.url.params["startblock"] for r in handler.requests]
    assert startblocks[:2] == ["0"] * 2
    assert startblocks[2] == "104"


async def test_raises_on_non_retryable_api_error() -> None:
    handler = _Handler([{"status": "0", "message": "NOTOK", "result": "Invalid API Key"}])
    client = _client(handler)

    with pytest.raises(EtherscanError, match="NOTOK"):
        await client.get_normal_transactions("0xabc")


async def test_retries_on_server_error_then_succeeds() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    responses = [
        httpx.Response(503, text="upstream error"),
        httpx.Response(200, json=_ok([_tx("1", 100)])),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = EtherscanClient(
        "fake-key",
        http_client=http_client,
        rate_limiter=TokenBucketRateLimiter(rate_per_sec=1000),
        backoff_base_seconds=0.001,
        sleep=fake_sleep,
    )

    result = await client.get_normal_transactions("0xabc")

    assert [t.hash for t in result] == ["0x1"]
    assert sleeps == [0.001]


async def test_raises_after_exhausting_retries() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream error")

    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    client = EtherscanClient(
        "fake-key",
        http_client=http_client,
        rate_limiter=TokenBucketRateLimiter(rate_per_sec=1000),
        max_retries=3,
        backoff_base_seconds=0.001,
        sleep=fake_sleep,
    )

    with pytest.raises(EtherscanError, match="after 3 attempts"):
        await client.get_normal_transactions("0xabc")

    assert sleeps == [0.001, 0.002]
