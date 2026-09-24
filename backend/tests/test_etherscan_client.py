"""Tests for EtherscanClient against a mocked transport (no live API calls)."""

from collections.abc import Awaitable, Callable

import httpx
import pytest

from app.clients.etherscan import Endpoint, EtherscanClient, EtherscanError
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

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc")

    assert result.transfers == []
    assert result.request_count == 1


async def test_parses_transactions_from_a_single_page() -> None:
    handler = _Handler([_ok([_tx("1", 100), _tx("2", 101)])])
    client = _client(handler)

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc")

    assert [t.hash for t in result.transfers] == ["0x1", "0x2"]
    assert result.transfers[0].block_number == 100
    assert result.transfers[0].value == 1_000_000_000_000_000_000


async def test_paginates_within_the_10000_record_window() -> None:
    full_page = _ok([_tx(str(i), 100 + i) for i in range(3)])
    partial_page = _ok([_tx("last", 200)])
    handler = _Handler([full_page, partial_page])
    client = _client(handler)

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc", page_size=3)

    assert len(result.transfers) == 4
    assert result.request_count == 2
    assert [r.url.params["page"] for r in handler.requests] == ["1", "2"]


async def test_rewindows_from_last_block_without_losing_a_split_block() -> None:
    # page_size=3, window=6: after 2 full pages the client must re-window. The window
    # ends mid-block (104a read, 104b not yet), so it must restart AT block 104.
    window_one_pages = [
        _ok([_tx("100", 100), _tx("101", 101), _tx("102", 102)]),
        _ok([_tx("103", 103), _tx("104", 104), _tx("104a", 104)]),
    ]
    window_two_pages = [
        _ok([_tx("104a", 104), _tx("104b", 104), _tx("200", 200)]),
        _no_transactions(),
    ]
    handler = _Handler([*window_one_pages, *window_two_pages])
    client = _client(handler, max_record_window=6)

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc", page_size=3)

    hashes = [t.hash for t in result.transfers]
    assert hashes == ["0x100", "0x101", "0x102", "0x103", "0x104", "0x104a", "0x104b", "0x200"]
    startblocks = [r.url.params["startblock"] for r in handler.requests]
    assert startblocks == ["0", "0", "104", "104"]


async def test_window_entirely_inside_one_block_still_terminates() -> None:
    same_block = [_ok([_tx("a", 100), _tx("b", 100)]), _ok([_tx("c", 101)])]
    handler = _Handler(same_block)
    client = _client(handler, max_record_window=2)

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc", startblock=100, page_size=2)

    assert [t.hash for t in result.transfers] == ["0xa", "0xb", "0xc"]
    assert [r.url.params["startblock"] for r in handler.requests] == ["100", "101"]


async def test_raises_on_non_retryable_api_error() -> None:
    handler = _Handler([{"status": "0", "message": "NOTOK", "result": "Invalid API Key"}])
    client = _client(handler)

    with pytest.raises(EtherscanError, match="NOTOK"):
        await client.fetch_transfers(Endpoint.NORMAL, "0xabc")


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

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc")

    assert [t.hash for t in result.transfers] == ["0x1"]
    assert sleeps == [0.001]
    assert result.request_count == 2


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
        await client.fetch_transfers(Endpoint.NORMAL, "0xabc")

    assert sleeps == [0.001, 0.002]


async def test_retries_when_etherscan_reports_rate_limit() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    rate_limited: dict[str, object] = {
        "status": "0",
        "message": "NOTOK",
        "result": "Max calls per sec rate limit reached (3/sec)",
    }
    handler = _Handler([rate_limited, _ok([_tx("1", 100)])])
    client = _client(handler, sleep=fake_sleep)

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc")

    assert [t.hash for t in result.transfers] == ["0x1"]
    assert result.request_count == 2
    assert sleeps == [0.001]


async def test_does_not_retry_client_errors() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, text="bad request")

    client = EtherscanClient(
        "fake-key",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        rate_limiter=TokenBucketRateLimiter(rate_per_sec=1000),
    )

    with pytest.raises(EtherscanError, match="HTTP 400"):
        await client.fetch_transfers(Endpoint.NORMAL, "0xabc")

    assert calls == 1


async def test_contract_creation_uses_the_new_contract_as_counterparty() -> None:
    creation = {**_tx("1", 100), "to": "", "contractAddress": "0xNewContract"}
    handler = _Handler([_ok([creation])])
    client = _client(handler)

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc")

    assert result.transfers[0].to_address == "0xnewcontract"
    assert result.transfers[0].token_address is None


async def test_internal_transfers_are_keyed_by_trace_id() -> None:
    parent = {**_tx("aa", 100), "traceId": "0_1"}
    sibling = {**_tx("aa", 100), "traceId": "0_2"}
    handler = _Handler([_ok([parent, sibling])])
    client = _client(handler)

    result = await client.fetch_transfers(Endpoint.INTERNAL, "0xabc")

    assert [t.sub_key for t in result.transfers] == ["0_1", "0_2"]
    assert handler.requests[0].url.params["action"] == "txlistinternal"


def _token(hash_suffix: str, block: int, value: str = "5") -> dict[str, object]:
    return {
        **_tx(hash_suffix, block),
        "value": value,
        "contractAddress": "0xToken",
        "tokenSymbol": "USDC",
        "tokenDecimal": "6",
    }


async def test_token_transfers_carry_token_fields() -> None:
    handler = _Handler([_ok([_token("1", 100)])])
    client = _client(handler)

    result = await client.fetch_transfers(Endpoint.TOKEN, "0xabc")

    transfer = result.transfers[0]
    assert transfer.token_address == "0xtoken"
    assert transfer.token_symbol == "USDC"
    assert transfer.token_decimals == 6
    assert transfer.to_address == "0xto"
    assert handler.requests[0].url.params["action"] == "tokentx"


async def test_identical_token_transfers_in_one_tx_are_both_kept() -> None:
    handler = _Handler([_ok([_token("1", 100), _token("1", 100), _token("1", 100, value="7")])])
    client = _client(handler)

    result = await client.fetch_transfers(Endpoint.TOKEN, "0xabc")

    keys = [t.sub_key for t in result.transfers]
    assert len(set(keys)) == 3
    assert keys[1] == keys[0] + "#1"


async def test_repeated_transfers_split_across_windows_are_not_double_counted() -> None:
    # Window 1 ends after the first of two identical transfers in block 101. Window 2
    # re-reads block 101 from its start, so both get the same keys as a single read would.
    window_one = [_ok([_token("0", 100), _token("1", 101)])]
    window_two = [_ok([_token("1", 101), _token("1", 101)]), _no_transactions()]
    handler = _Handler([*window_one, *window_two])
    client = _client(handler, max_record_window=2)

    result = await client.fetch_transfers(Endpoint.TOKEN, "0xabc", page_size=2)

    assert len(result.transfers) == 3
    # Window 2 is entirely block 101, so the client then moves on to block 102.
    assert [r.url.params["startblock"] for r in handler.requests] == ["0", "101", "102"]


async def test_max_records_stops_early_and_flags_truncation() -> None:
    pages = [_ok([_tx(f"{i}a", i), _tx(f"{i}b", i)]) for i in range(1, 4)]
    handler = _Handler(pages)
    client = _client(handler)

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc", page_size=2, max_records=3)

    assert result.truncated
    assert len(result.transfers) == 4
    assert len(handler.requests) == 2


async def test_complete_history_under_the_cap_is_not_truncated() -> None:
    handler = _Handler([_ok([_tx("1", 100)])])
    client = _client(handler)

    result = await client.fetch_transfers(Endpoint.NORMAL, "0xabc", max_records=3)

    assert not result.truncated
