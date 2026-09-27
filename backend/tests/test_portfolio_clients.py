"""Tests for the retry helper and the DefiLlama and Alchemy clients (mocked transports)."""

import asyncio
import json

import httpx
import pytest

from app.clients.balances import BalanceClient, BalanceError
from app.clients.prices import ETH_PRICE_ID, PriceClient, PriceError, token_price_id
from app.core.retry import RetryError, RetryPolicy, send_with_retry

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
SPAM = "0x" + "5" * 40


async def _no_sleep(seconds: float) -> None:
    return None


FAST = RetryPolicy(max_retries=3, backoff_base_seconds=0, sleep=_no_sleep)


def _client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def test_retry_recovers_from_5xx_and_429() -> None:
    responses = [httpx.Response(503), httpx.Response(429), httpx.Response(200, text="ok")]
    http = _client(httpx.MockTransport(lambda r: responses.pop(0)))

    response = await send_with_retry(lambda: http.get("https://x.test"), what="x", policy=FAST)

    assert response.text == "ok"


async def test_retry_fails_fast_on_other_4xx() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401)

    http = _client(httpx.MockTransport(handler))

    with pytest.raises(RetryError, match="HTTP 401"):
        await send_with_retry(lambda: http.get("https://x.test"), what="x", policy=FAST)
    assert len(calls) == 1


async def test_retry_gives_up_after_max_retries() -> None:
    http = _client(httpx.MockTransport(lambda r: httpx.Response(500)))

    with pytest.raises(RetryError, match="after 3 attempts"):
        await send_with_retry(lambda: http.get("https://x.test"), what="x", policy=FAST)


def _llama(coins: dict[str, dict[str, object]]) -> httpx.Response:
    return httpx.Response(200, json={"coins": coins})


async def test_prices_parse_and_omit_unknown_tokens() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return _llama(
            {
                ETH_PRICE_ID: {"price": 2688.4, "symbol": "ETH", "confidence": 0.99},
                token_price_id(USDC): {
                    "price": 0.9998,
                    "symbol": "USDC",
                    "decimals": 6,
                    "confidence": 0.99,
                },
            }
        )

    client = PriceClient(_client(httpx.MockTransport(handler)), retry=FAST)

    prices = await client.get_prices([ETH_PRICE_ID, token_price_id(USDC), token_price_id(SPAM)])

    assert prices[token_price_id(USDC)].decimals == 6
    assert prices[ETH_PRICE_ID].price_usd == 2688.4
    assert token_price_id(SPAM) not in prices
    assert len(seen) == 1


async def test_prices_are_requested_in_batches() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return _llama({})

    client = PriceClient(_client(httpx.MockTransport(handler)), retry=FAST)
    ids = [token_price_id(f"0x{i:040x}") for i in range(120)]

    await client.get_prices(ids)

    assert len(requests) == 3
    assert all(path.count(",") < 50 for path in requests)


async def test_price_outage_raises_price_error() -> None:
    client = PriceClient(_client(httpx.MockTransport(lambda r: httpx.Response(502))), retry=FAST)

    with pytest.raises(PriceError):
        await client.get_prices([ETH_PRICE_ID])


def _alchemy(balances: list[dict[str, object]], page_key: str | None = None) -> httpx.Response:
    result: dict[str, object] = {"address": "0x1", "tokenBalances": balances}
    if page_key:
        result["pageKey"] = page_key
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


async def test_balances_paginate_and_skip_zero_and_errored_entries() -> None:
    bodies: list[dict[str, object]] = []
    pages = [
        _alchemy(
            [
                {
                    "contractAddress": USDC.upper().replace("0X", "0x"),
                    "tokenBalance": hex(5_000_000),
                },
                {"contractAddress": SPAM, "tokenBalance": "0x" + "0" * 64},
            ],
            page_key="next",
        ),
        _alchemy(
            [
                {"contractAddress": "0x" + "7" * 40, "tokenBalance": None, "error": "bad token"},
                {"contractAddress": "0x" + "8" * 40, "tokenBalance": hex(42)},
            ]
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return pages.pop(0)

    client = BalanceClient("key", _client(httpx.MockTransport(handler)), retry=FAST)

    balances = await client.token_balances("0x" + "1" * 40)

    assert balances == {USDC: 5_000_000, "0x" + "8" * 40: 42}
    assert bodies[0]["params"] == ["0x" + "1" * 40, "erc20", {"maxCount": 100}]
    assert bodies[1]["params"][2] == {"maxCount": 100, "pageKey": "next"}  # type: ignore[index]


async def test_balances_stop_at_max_pages() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _alchemy([{"contractAddress": SPAM, "tokenBalance": "0x1"}], page_key="more")

    client = BalanceClient("key", _client(httpx.MockTransport(handler)), max_pages=3, retry=FAST)

    await client.token_balances("0x" + "1" * 40)

    assert len(calls) == 3


async def test_balances_json_rpc_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "bad key"}}
        )

    client = BalanceClient("key", _client(httpx.MockTransport(handler)), retry=FAST)

    with pytest.raises(BalanceError, match="bad key"):
        await client.token_balances("0x" + "1" * 40)


async def test_api_key_goes_in_the_url_path_only() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return _alchemy([])

    client = BalanceClient("SECRET", _client(httpx.MockTransport(handler)), retry=FAST)

    await client.token_balances("0x" + "1" * 40)

    assert urls == ["https://eth-mainnet.g.alchemy.com/v2/SECRET"]


async def test_price_batches_run_concurrently_but_bounded() -> None:
    active = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return _llama({})

    client = PriceClient(_client(httpx.MockTransport(handler)), retry=FAST)

    await client.get_prices([token_price_id(f"0x{i:040x}") for i in range(500)])

    assert peak == 4
