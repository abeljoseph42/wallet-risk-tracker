"""Async client for the Etherscan V2 API.

Confirmed against docs.etherscan.io (2026-09): base URL is
`https://api.etherscan.io/v2/api`, chains are selected via `chainid` (1 =
Ethereum mainnet), and the free tier caps each `txlist` page at 1,000
records with a hard 10,000-record window per (address, block range) pair.
To read a full history beyond that window we page within it, then advance
`startblock` past the last block seen and keep going.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, Field

from app.core.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)

ETHERSCAN_BASE_URL = "https://api.etherscan.io/v2/api"
ETHEREUM_MAINNET_CHAIN_ID = 1

# Etherscan's documented cap on (page * offset) per (address, block-range) query.
_MAX_RECORD_WINDOW = 10_000
_NO_TRANSACTIONS_MESSAGE = "No transactions found"


class EtherscanError(Exception):
    """Raised for non-retryable Etherscan API errors (bad key, bad address, etc.)."""


class EtherscanTransaction(BaseModel):
    hash: str
    block_number: int = Field(alias="blockNumber")
    timestamp: int = Field(alias="timeStamp")
    from_address: str = Field(alias="from")
    to_address: str = Field(alias="to")
    # Set (and `to` empty) when the transaction deployed a contract.
    contract_address: str = Field(alias="contractAddress", default="")
    value_wei: int = Field(alias="value")
    is_error: bool = Field(alias="isError")
    gas_used: int = Field(alias="gasUsed")

    model_config = {"populate_by_name": True}


@dataclass(frozen=True)
class FetchedTransactions:
    transactions: list[EtherscanTransaction]
    # HTTP requests sent for this fetch, retries included. Returned per call rather than
    # kept as a shared counter so concurrent fetches on one client report correctly.
    request_count: int


class EtherscanClient:
    """Fetches transaction history from Etherscan, rate-limited and retried."""

    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.AsyncClient,
        rate_limiter: TokenBucketRateLimiter,
        base_url: str = ETHERSCAN_BASE_URL,
        chain_id: int = ETHEREUM_MAINNET_CHAIN_ID,
        max_retries: int = 5,
        backoff_base_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        max_record_window: int = _MAX_RECORD_WINDOW,
    ) -> None:
        self._api_key = api_key
        self._http = http_client
        self._rate_limiter = rate_limiter
        self._base_url = base_url
        self._chain_id = chain_id
        self._max_retries = max_retries
        self._backoff_base = backoff_base_seconds
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._max_record_window = max_record_window

    async def get_normal_transactions(
        self,
        address: str,
        *,
        startblock: int = 0,
        endblock: int = 99_999_999,
        page_size: int = 1_000,
    ) -> FetchedTransactions:
        """Return all normal transactions for `address` in [startblock, endblock].

        Pages within Etherscan's 10,000-record window using `page`/`offset`,
        then advances `startblock` past the last block seen to continue
        reading histories larger than the window.
        """
        transactions: list[EtherscanTransaction] = []
        requests = 0
        window_start = startblock

        while window_start <= endblock:
            page = 1
            last_block_in_window: int | None = None

            while page * page_size <= self._max_record_window:
                batch, attempts = await self._fetch_txlist_page(
                    address,
                    startblock=window_start,
                    endblock=endblock,
                    page=page,
                    offset=page_size,
                )
                requests += attempts
                if not batch:
                    break
                transactions.extend(batch)
                last_block_in_window = batch[-1].block_number
                if len(batch) < page_size:
                    return FetchedTransactions(transactions, requests)
                page += 1

            if last_block_in_window is None:
                break
            window_start = last_block_in_window + 1

        return FetchedTransactions(transactions, requests)

    async def _fetch_txlist_page(
        self,
        address: str,
        *,
        startblock: int,
        endblock: int,
        page: int,
        offset: int,
    ) -> tuple[list[EtherscanTransaction], int]:
        params: dict[str, str | int] = {
            "chainid": self._chain_id,
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": startblock,
            "endblock": endblock,
            "page": page,
            "offset": offset,
            "sort": "asc",
            "apikey": self._api_key,
        }
        payload, attempts = await self._request(params)

        status = payload.get("status")
        message = payload.get("message", "")
        result = payload.get("result")

        if status == "0":
            if message == _NO_TRANSACTIONS_MESSAGE:
                return [], attempts
            raise EtherscanError(f"Etherscan error for {address}: {message}: {result}")

        if not isinstance(result, list):
            raise EtherscanError(f"Unexpected Etherscan payload for {address}: {payload}")

        return [EtherscanTransaction.model_validate(item) for item in result], attempts

    async def _request(self, params: dict[str, str | int]) -> tuple[dict[str, object], int]:
        """GET with retry; returns the JSON payload and the number of attempts made."""
        last_error: str = ""
        for attempt in range(1, self._max_retries + 1):
            await self._rate_limiter.acquire()
            try:
                response = await self._http.get(self._base_url, params=params)
            except httpx.TransportError as exc:
                last_error = f"transport error: {exc}"
            else:
                if response.status_code >= 500 or response.status_code == 429:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    raise EtherscanError(f"Etherscan returned HTTP {response.status_code}")
                else:
                    data: dict[str, object] = response.json()
                    if not _is_rate_limited(data):
                        return data, attempt
                    last_error = f"rate limited: {data.get('result')}"

            if attempt == self._max_retries:
                break
            delay = self._backoff_base * (2 ** (attempt - 1))
            logger.warning(
                "Etherscan request failed (attempt %d/%d), retrying in %.2fs: %s",
                attempt,
                self._max_retries,
                delay,
                last_error,
            )
            await self._sleep(delay)

        raise EtherscanError(
            f"Etherscan request failed after {self._max_retries} attempts: {last_error}"
        )


def _is_rate_limited(payload: dict[str, object]) -> bool:
    # Etherscan signals throttling with HTTP 200, status "0", and a result such as
    # "Max calls per sec rate limit reached (3/sec)".
    result = payload.get("result")
    return (
        payload.get("status") == "0" and isinstance(result, str) and "rate limit" in result.lower()
    )
