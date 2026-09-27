"""USD prices from DefiLlama's free coins API (no API key).

Verified 2026-09-27: `GET https://coins.llama.fi/prices/current/{ids}` takes a
comma-separated list of ids ("coingecko:ethereum", "ethereum:0x<token>") and returns
price, symbol, decimals and a 0-1 confidence per id. Ids it can't price are simply
absent from the response, which also filters out most spam tokens.
"""

from dataclasses import dataclass
from functools import partial

import httpx

from app.core.retry import RetryError, RetryPolicy, send_with_retry

DEFILLAMA_BASE_URL = "https://coins.llama.fi"
ETH_PRICE_ID = "coingecko:ethereum"
# Keeps request URLs well under common length limits (each id is ~51 characters).
_BATCH_SIZE = 50


class PriceError(Exception):
    pass


def token_price_id(token_address: str) -> str:
    return f"ethereum:{token_address.lower()}"


@dataclass(frozen=True)
class TokenPrice:
    price_usd: float
    symbol: str | None
    decimals: int | None
    confidence: float | None


class PriceClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        base_url: str = DEFILLAMA_BASE_URL,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._retry = retry or RetryPolicy()

    async def get_prices(self, ids: list[str]) -> dict[str, TokenPrice]:
        """Prices for the ids DefiLlama knows; unknown ids are left out."""
        prices: dict[str, TokenPrice] = {}
        unique = sorted(set(ids))
        for start in range(0, len(unique), _BATCH_SIZE):
            batch = unique[start : start + _BATCH_SIZE]
            url = f"{self._base_url}/prices/current/{','.join(batch)}"
            try:
                response = await send_with_retry(
                    partial(self._http.get, url), what="DefiLlama prices", policy=self._retry
                )
                coins = response.json().get("coins", {})
            except (RetryError, ValueError) as exc:
                raise PriceError(str(exc)) from exc
            for coin_id, data in coins.items():
                if not isinstance(data, dict) or data.get("price") is None:
                    continue
                prices[coin_id] = TokenPrice(
                    price_usd=float(data["price"]),
                    symbol=data.get("symbol"),
                    decimals=int(data["decimals"]) if data.get("decimals") is not None else None,
                    confidence=float(data["confidence"])
                    if data.get("confidence") is not None
                    else None,
                )
        return prices
