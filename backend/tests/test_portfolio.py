"""Tests for the TTL cache and the portfolio service (fake providers, no network)."""

from decimal import Decimal

import pytest

from app.clients.balances import BalanceError
from app.clients.etherscan import EtherscanError
from app.clients.prices import ETH_PRICE_ID, PriceError, TokenPrice, token_price_id
from app.core.ttl_cache import TTLCache
from app.services.portfolio import PortfolioService

WALLET = "0x" + "1" * 40
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
DAI = "0x6b175474e89094c44da98b954eedeac495271d0f"
SPAM = "0x" + "5" * 40
SHADY = "0x" + "6" * 40
ETH = 10**18


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_ttl_cache_expires_and_caps_size() -> None:
    clock = Clock()
    cache: TTLCache[str, int | None] = TTLCache(10, max_entries=2, clock=clock)
    cache.set("a", 1)
    cache.set("none", None)

    assert cache.get("a") == (True, 1)
    assert cache.get("none") == (True, None)
    assert cache.get("missing") == (False, None)

    cache.set("c", 3)  # evicts the oldest entry ("a")
    assert cache.get("a") == (False, None)
    assert len(cache) == 2

    clock.now = 10
    assert cache.get("c") == (False, None)


class FakeEth:
    def __init__(self, wei: int = 2 * ETH, fail: bool = False) -> None:
        self.wei, self.fail, self.calls = wei, fail, 0

    async def eth_balance(self, address: str) -> int:
        self.calls += 1
        if self.fail:
            raise EtherscanError("down", kind="unavailable")
        return self.wei


class FakeTokens:
    def __init__(self, balances: dict[str, int] | None = None, fail: bool = False) -> None:
        self.balances = balances if balances is not None else {}
        self.fail, self.calls = fail, 0

    async def token_balances(self, address: str) -> dict[str, int]:
        self.calls += 1
        if self.fail:
            raise BalanceError("down")
        return self.balances


class FakePrices:
    def __init__(self, prices: dict[str, TokenPrice], fail: bool = False) -> None:
        self.prices, self.fail = prices, fail
        self.requested: list[list[str]] = []

    async def get_prices(self, ids: list[str]) -> dict[str, TokenPrice]:
        self.requested.append(sorted(ids))
        if self.fail:
            raise PriceError("down")
        return {i: self.prices[i] for i in ids if i in self.prices}


PRICES = {
    ETH_PRICE_ID: TokenPrice(2000.0, "ETH", None, 0.99),
    token_price_id(USDC): TokenPrice(1.0, "USDC", 6, 0.99),
    token_price_id(DAI): TokenPrice(1.0, "DAI", 18, 0.99),
    token_price_id(SHADY): TokenPrice(500.0, "SHADY", 18, 0.4),  # fake-pool price
}
BALANCES = {USDC: 3_000 * 10**6, DAI: 50 * 10**18, SPAM: 10**24, SHADY: 10**20}


def _service(
    eth: FakeEth | None = None,
    tokens: FakeTokens | None = None,
    prices: FakePrices | None = None,
    **kwargs: int,
) -> PortfolioService:
    return PortfolioService(
        eth if eth is not None else FakeEth(),
        tokens if tokens is not None else FakeTokens(BALANCES),
        prices if prices is not None else FakePrices(PRICES),
        **kwargs,
    )


async def test_values_eth_and_priced_tokens_and_counts_the_rest() -> None:
    portfolio = await _service().get(WALLET)

    assert portfolio.eth is not None
    assert portfolio.eth.balance == Decimal(2)
    assert portfolio.eth.value_usd == 4000.0
    assert [t.symbol for t in portfolio.tokens] == ["USDC", "DAI"]
    assert portfolio.tokens[0].value_usd == pytest.approx(3000.0)
    # SPAM has no price; SHADY's price has confidence 0.4.
    assert portfolio.unpriced_token_count == 2
    assert portfolio.priced_token_count == 2
    assert portfolio.total_usd == pytest.approx(4000 + 3000 + 50)
    assert portfolio.warnings == []


async def test_top_n_limits_the_list_but_not_the_total() -> None:
    portfolio = await _service(top_n=1).get(WALLET)

    assert [t.symbol for t in portfolio.tokens] == ["USDC"]
    assert portfolio.priced_token_count == 2
    assert portfolio.total_usd == pytest.approx(7050)


async def test_balances_and_prices_are_cached_including_missing_prices() -> None:
    eth, tokens, prices = FakeEth(), FakeTokens(BALANCES), FakePrices(PRICES)
    service = _service(eth, tokens, prices)

    await service.get(WALLET)
    await service.get(WALLET)

    assert (eth.calls, tokens.calls) == (1, 1)
    # The second request finds every id cached, SPAM's "no price" included.
    assert len(prices.requested) == 1


async def test_price_outage_still_returns_balances() -> None:
    portfolio = await _service(prices=FakePrices(PRICES, fail=True)).get(WALLET)

    assert portfolio.warnings == ["prices_unavailable"]
    assert portfolio.eth is not None and portfolio.eth.price_usd is None
    assert portfolio.tokens == []
    assert portfolio.unpriced_token_count == len(BALANCES)
    assert portfolio.total_usd is None


async def test_token_balance_outage_still_returns_eth() -> None:
    portfolio = await _service(tokens=FakeTokens(fail=True)).get(WALLET)

    assert portfolio.warnings == ["token_balances_unavailable"]
    assert portfolio.total_usd == 4000.0


async def test_eth_balance_outage_still_returns_tokens() -> None:
    portfolio = await _service(eth=FakeEth(fail=True)).get(WALLET)

    assert portfolio.warnings == ["eth_balance_unavailable"]
    assert portfolio.eth is None
    assert portfolio.total_usd == pytest.approx(3050)


async def test_missing_alchemy_key_is_reported_not_raised() -> None:
    service = PortfolioService(FakeEth(), None, FakePrices(PRICES))

    portfolio = await service.get(WALLET)

    assert portfolio.warnings == ["token_balances_not_configured"]
    assert portfolio.total_usd == 4000.0


async def test_failures_are_not_cached() -> None:
    tokens = FakeTokens(BALANCES, fail=True)
    service = _service(tokens=tokens)
    await service.get(WALLET)

    tokens.fail = False
    portfolio = await service.get(WALLET)

    assert portfolio.warnings == []
    assert tokens.calls == 2


async def test_invalid_address_is_rejected() -> None:
    with pytest.raises(ValueError, match="Not a valid"):
        await _service().get("0x123")
