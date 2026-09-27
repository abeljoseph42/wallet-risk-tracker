"""ERC-20 balances from Alchemy's Token API (free tier).

Verified 2026-09-27: JSON-RPC `alchemy_getTokenBalances` at
`https://eth-mainnet.g.alchemy.com/v2/{key}`, params `[address, "erc20", {pageKey,
maxCount<=100}]`, 20 compute units per call (free tier: 30M CU/month, 300 CU/s).
The key is in the URL, so the URL must never be logged (httpx request logging is off).
"""

import itertools
from functools import partial

import httpx

from app.core.retry import RetryError, RetryPolicy, send_with_retry

ALCHEMY_ETH_MAINNET_URL = "https://eth-mainnet.g.alchemy.com/v2/{key}"
_PAGE_SIZE = 100


class BalanceError(Exception):
    pass


class BalanceClient:
    def __init__(
        self,
        api_key: str,
        http: httpx.AsyncClient,
        *,
        url_template: str = ALCHEMY_ETH_MAINNET_URL,
        max_pages: int = 20,
        retry: RetryPolicy | None = None,
    ) -> None:
        self._url = url_template.format(key=api_key)
        self._http = http
        # A wallet spammed with thousands of airdrops shouldn't cost unbounded calls.
        self._max_pages = max_pages
        self._retry = retry or RetryPolicy()

    async def token_balances(self, address: str) -> dict[str, int]:
        """Token contract -> raw balance, for every non-zero ERC-20 balance."""
        balances: dict[str, int] = {}
        page_key: str | None = None
        for request_id in itertools.count(1):
            options: dict[str, object] = {"maxCount": _PAGE_SIZE}
            if page_key:
                options["pageKey"] = page_key
            body = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "alchemy_getTokenBalances",
                "params": [address, "erc20", options],
            }
            try:
                response = await send_with_retry(
                    partial(self._http.post, self._url, json=body),
                    what="Alchemy getTokenBalances",
                    policy=self._retry,
                )
                payload = response.json()
            except (RetryError, ValueError) as exc:
                raise BalanceError(str(exc)) from exc
            if "error" in payload:
                raise BalanceError(f"Alchemy error: {payload['error'].get('message')}")
            result = payload.get("result") or {}
            for item in result.get("tokenBalances", []):
                raw = item.get("tokenBalance")
                if item.get("error") or not raw:
                    continue
                value = int(raw, 16)
                if value > 0:
                    balances[str(item["contractAddress"]).lower()] = value
            page_key = result.get("pageKey")
            if not page_key or request_id >= self._max_pages:
                return balances
        return balances
