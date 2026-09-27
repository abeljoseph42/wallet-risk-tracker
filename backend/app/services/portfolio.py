"""Wallet holdings with USD values: ETH plus priced ERC-20 tokens.

Sources: ETH balance from Etherscan, token balances from Alchemy, prices from DefiLlama.
Each can fail independently; the portfolio then returns what it still knows, with a
warning naming what's missing, instead of failing the whole request.

Tokens without a DefiLlama price, or priced with confidence below MIN_PRICE_CONFIDENCE,
are counted but not listed: that's almost always airdropped spam, and a fake pool can
give a spam token a large "price".
"""

import datetime
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.clients.balances import BalanceError
from app.clients.etherscan import EtherscanError
from app.clients.prices import ETH_PRICE_ID, PriceError, TokenPrice, token_price_id
from app.core.addresses import normalize_address
from app.core.ttl_cache import TTLCache

MIN_PRICE_CONFIDENCE = 0.9
ETH_DECIMALS = 18


class EthBalanceSource(Protocol):
    async def eth_balance(self, address: str) -> int: ...


class TokenBalanceSource(Protocol):
    async def token_balances(self, address: str) -> dict[str, int]: ...


class PriceSource(Protocol):
    async def get_prices(self, ids: list[str]) -> dict[str, TokenPrice]: ...


@dataclass(frozen=True)
class Holding:
    token_address: str | None  # None for ETH
    symbol: str
    decimals: int
    balance_raw: int
    price_usd: float | None
    price_confidence: float | None

    @property
    def balance(self) -> Decimal:
        return Decimal(self.balance_raw) / (Decimal(10) ** self.decimals)

    @property
    def value_usd(self) -> float | None:
        return None if self.price_usd is None else float(self.balance) * self.price_usd


@dataclass(frozen=True)
class Portfolio:
    address: str
    eth: Holding | None
    # Priced tokens by USD value, largest first, at most `top_n`.
    tokens: list[Holding]
    priced_token_count: int
    unpriced_token_count: int
    # ETH plus every priced token (not only the listed ones); None without prices.
    total_usd: float | None
    warnings: list[str]
    as_of: datetime.datetime


class PortfolioService:
    def __init__(
        self,
        eth: EthBalanceSource | None,
        tokens: TokenBalanceSource | None,
        prices: PriceSource,
        *,
        cache_seconds: float = 300,
        top_n: int = 20,
    ) -> None:
        self._eth = eth
        self._tokens = tokens
        self._prices = prices
        self._top_n = top_n
        self._eth_cache: TTLCache[str, int] = TTLCache(cache_seconds)
        self._token_cache: TTLCache[str, dict[str, int]] = TTLCache(cache_seconds)
        self._price_cache: TTLCache[str, TokenPrice | None] = TTLCache(cache_seconds)

    async def get(self, address: str) -> Portfolio:
        address = normalize_address(address)
        warnings: list[str] = []
        eth_raw = await self._eth_balance(address, warnings)
        token_raw = await self._token_balances(address, warnings)

        ids = [ETH_PRICE_ID, *(token_price_id(t) for t in token_raw)]
        try:
            prices = await self._prices_for(ids)
        except PriceError:
            warnings.append("prices_unavailable")
            prices = None

        eth = None
        if eth_raw is not None:
            eth_price = prices.get(ETH_PRICE_ID) if prices is not None else None
            eth = Holding(
                token_address=None,
                symbol="ETH",
                decimals=ETH_DECIMALS,
                balance_raw=eth_raw,
                price_usd=eth_price.price_usd if eth_price else None,
                price_confidence=eth_price.confidence if eth_price else None,
            )

        priced: list[Holding] = []
        for token, raw in token_raw.items():
            price = prices.get(token_price_id(token)) if prices is not None else None
            if price is None or price.decimals is None:
                continue
            if price.confidence is not None and price.confidence < MIN_PRICE_CONFIDENCE:
                continue
            priced.append(
                Holding(
                    token_address=token,
                    symbol=price.symbol or "?",
                    decimals=price.decimals,
                    balance_raw=raw,
                    price_usd=price.price_usd,
                    price_confidence=price.confidence,
                )
            )
        priced.sort(key=lambda h: (-(h.value_usd or 0.0), h.token_address or ""))

        total = None
        if prices is not None:
            values = [h.value_usd for h in [*priced, *([eth] if eth else [])]]
            total = sum(v for v in values if v is not None)
        return Portfolio(
            address=address,
            eth=eth,
            tokens=priced[: self._top_n],
            priced_token_count=len(priced),
            unpriced_token_count=len(token_raw) - len(priced),
            total_usd=total,
            warnings=warnings,
            as_of=datetime.datetime.now(datetime.UTC),
        )

    async def _eth_balance(self, address: str, warnings: list[str]) -> int | None:
        hit, cached = self._eth_cache.get(address)
        if hit:
            return cached
        if self._eth is None:
            warnings.append("eth_balance_not_configured")
            return None
        try:
            value = await self._eth.eth_balance(address)
        except EtherscanError:
            warnings.append("eth_balance_unavailable")
            return None
        self._eth_cache.set(address, value)
        return value

    async def _token_balances(self, address: str, warnings: list[str]) -> dict[str, int]:
        hit, cached = self._token_cache.get(address)
        if hit and cached is not None:
            return cached
        if self._tokens is None:
            warnings.append("token_balances_not_configured")
            return {}
        try:
            value = await self._tokens.token_balances(address)
        except BalanceError:
            warnings.append("token_balances_unavailable")
            return {}
        self._token_cache.set(address, value)
        return value

    async def _prices_for(self, ids: list[str]) -> dict[str, TokenPrice]:
        result: dict[str, TokenPrice] = {}
        missing = []
        for coin_id in ids:
            hit, cached = self._price_cache.get(coin_id)
            if not hit:
                missing.append(coin_id)
            elif cached is not None:
                result[coin_id] = cached
        if missing:
            fetched = await self._prices.get_prices(missing)
            for coin_id in missing:
                # Cache "no price" too, so spam tokens aren't looked up on every request.
                price = fetched.get(coin_id)
                self._price_cache.set(coin_id, price)
                if price is not None:
                    result[coin_id] = price
        return result
