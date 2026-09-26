"""Async client for the Etherscan V2 API.

Confirmed against docs.etherscan.io (2026-09): base URL is
`https://api.etherscan.io/v2/api`, chains are selected via `chainid` (1 =
Ethereum mainnet). On the free tier `txlist`, `txlistinternal` and `tokentx`
return at most 1,000 records per page and 10,000 per (address, block range)
query. To read a full history beyond that window we page within it, then
restart from the last block seen and keep going.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.core.rate_limiter import TokenBucketRateLimiter

logger = logging.getLogger(__name__)

ETHERSCAN_BASE_URL = "https://api.etherscan.io/v2/api"
ETHEREUM_MAINNET_CHAIN_ID = 1

# Etherscan's documented cap on (page * offset) per (address, block-range) query.
_MAX_RECORD_WINDOW = 10_000
# Max records per page on the free tier (Etherscan changelog, July 2026).
TXLIST_PAGE_SIZE = 1_000
_NO_TRANSACTIONS_MESSAGE = "No transactions found"


class Endpoint(StrEnum):
    NORMAL = "txlist"
    INTERNAL = "txlistinternal"
    TOKEN = "tokentx"


EtherscanErrorKind = Literal["api", "rate_limited", "unavailable", "config"]


class EtherscanError(Exception):
    """An Etherscan call that failed for good.

    `kind`: "api" (Etherscan rejected the request, e.g. a bad key), "rate_limited" (still
    throttled after every retry), "unavailable" (transport errors or 5xx after every
    retry), or "config" (no API key configured).
    """

    def __init__(self, message: str, kind: EtherscanErrorKind = "api") -> None:
        super().__init__(message)
        self.kind: EtherscanErrorKind = kind


class _RawTransfer(BaseModel):
    """The fields we use from a txlist / txlistinternal / tokentx result item."""

    hash: str
    block_number: int = Field(alias="blockNumber")
    timestamp: int = Field(alias="timeStamp")
    from_address: str = Field(alias="from")
    to_address: str = Field(alias="to")
    # For txlist/txlistinternal: the deployed contract when `to` is empty.
    # For tokentx: the token contract.
    contract_address: str = Field(alias="contractAddress", default="")
    value: int
    is_error: bool = Field(alias="isError", default=False)
    trace_id: str = Field(alias="traceId", default="")
    token_symbol: str | None = Field(alias="tokenSymbol", default=None)
    token_decimal: str | None = Field(alias="tokenDecimal", default=None)


@dataclass(frozen=True)
class Transfer:
    """One value movement, normalized across endpoints (addresses and hash lowercase)."""

    endpoint: Endpoint
    hash: str
    # Distinguishes several transfers inside one transaction: "" for normal txs, the
    # traceId for internal ones, token/from/to/value for token transfers (tokentx has no
    # log index), plus "#n" when identical transfers repeat within a transaction.
    sub_key: str
    block_number: int
    timestamp: int
    from_address: str
    to_address: str
    # Base units of the asset: wei for ETH, the token's smallest unit for tokens.
    value: int
    token_address: str | None
    token_symbol: str | None
    token_decimals: int | None
    is_error: bool


@dataclass(frozen=True)
class FetchedTransfers:
    transfers: list[Transfer]
    # HTTP requests sent for this fetch, retries included. Returned per call rather than
    # kept as a shared counter so concurrent fetches on one client report correctly.
    request_count: int
    # True when `max_records` stopped the fetch before the full history was read.
    truncated: bool = False


class EtherscanClient:
    """Fetches transfer history from Etherscan, rate-limited and retried."""

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

    async def fetch_transfers(
        self,
        endpoint: Endpoint,
        address: str,
        *,
        startblock: int = 0,
        endblock: int = 99_999_999,
        page_size: int = TXLIST_PAGE_SIZE,
        max_records: int | None = None,
    ) -> FetchedTransfers:
        """Return `address`'s transfers from `endpoint` in [startblock, endblock], oldest first.

        Pages within Etherscan's 10,000-record window using `page`/`offset`, then restarts
        from the last block seen to read histories larger than the window. Stops early
        (`truncated=True`) once `max_records` transfers have been read.
        """
        transfers: list[Transfer] = []
        seen: set[tuple[str, str]] = set()
        requests = 0
        window_start = startblock

        while window_start <= endblock:
            page = 1
            last_block_in_window: int | None = None
            # Occurrence counters restart per window: a window always re-reads the
            # boundary block from its first transfer, so numbering stays consistent.
            occurrences: dict[tuple[str, str], int] = {}

            while page * page_size <= self._max_record_window:
                batch, attempts = await self._fetch_page(
                    endpoint,
                    address,
                    startblock=window_start,
                    endblock=endblock,
                    page=page,
                    offset=page_size,
                )
                requests += attempts
                for raw in batch:
                    transfer = _normalize(endpoint, raw, occurrences)
                    uid = (transfer.hash, transfer.sub_key)
                    if uid not in seen:
                        seen.add(uid)
                        transfers.append(transfer)
                if len(batch) < page_size:
                    return FetchedTransfers(transfers, requests)
                if max_records is not None and len(transfers) >= max_records:
                    return FetchedTransfers(transfers, requests, truncated=True)
                last_block_in_window = batch[-1].block_number
                page += 1

            if last_block_in_window is None:
                break
            if last_block_in_window == window_start:
                # A whole window sits inside one block; block numbers can't page past it.
                logger.warning(
                    "Over %d %s records for %s in block %d; some may be missing",
                    self._max_record_window,
                    endpoint,
                    address,
                    window_start,
                )
                window_start += 1
            else:
                # Restart at the last block, not +1: the window may have ended mid-block.
                # Re-read transfers are dropped by the `seen` check above.
                window_start = last_block_in_window

        return FetchedTransfers(transfers, requests)

    async def _fetch_page(
        self,
        endpoint: Endpoint,
        address: str,
        *,
        startblock: int,
        endblock: int,
        page: int,
        offset: int,
    ) -> tuple[list[_RawTransfer], int]:
        params: dict[str, str | int] = {
            "chainid": self._chain_id,
            "module": "account",
            "action": endpoint.value,
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

        return [_RawTransfer.model_validate(item) for item in result], attempts

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
            f"Etherscan request failed after {self._max_retries} attempts: {last_error}",
            kind="rate_limited" if last_error.startswith("rate limited") else "unavailable",
        )


def _is_rate_limited(payload: dict[str, object]) -> bool:
    # Etherscan signals throttling with HTTP 200, status "0", and a result such as
    # "Max calls per sec rate limit reached (3/sec)".
    result = payload.get("result")
    return (
        payload.get("status") == "0" and isinstance(result, str) and "rate limit" in result.lower()
    )


def client_from_settings(settings: Settings, http: httpx.AsyncClient) -> EtherscanClient:
    if not settings.etherscan_api_key:
        raise EtherscanError("ETHERSCAN_API_KEY is not set", kind="config")
    rate = settings.etherscan_rate_limit_per_sec * settings.etherscan_rate_headroom
    return EtherscanClient(
        settings.etherscan_api_key,
        http_client=http,
        rate_limiter=TokenBucketRateLimiter(rate),
    )


def _normalize(
    endpoint: Endpoint, raw: _RawTransfer, occurrences: dict[tuple[str, str], int]
) -> Transfer:
    tx_hash = raw.hash.lower()
    from_address = raw.from_address.lower()
    contract = raw.contract_address.lower()
    if endpoint is Endpoint.TOKEN:
        to_address = raw.to_address.lower()
        token_address: str | None = contract
        base_key = f"{contract}:{from_address}:{to_address}:{raw.value}"
    else:
        to_address = (raw.to_address or raw.contract_address).lower()
        token_address = None
        base_key = raw.trace_id if endpoint is Endpoint.INTERNAL else ""

    n = occurrences.get((tx_hash, base_key), 0)
    occurrences[(tx_hash, base_key)] = n + 1
    decimals = raw.token_decimal
    return Transfer(
        endpoint=endpoint,
        hash=tx_hash,
        sub_key=base_key if n == 0 else f"{base_key}#{n}",
        block_number=raw.block_number,
        timestamp=raw.timestamp,
        from_address=from_address,
        to_address=to_address,
        value=raw.value,
        token_address=token_address,
        token_symbol=raw.token_symbol,
        token_decimals=int(decimals) if decimals and decimals.isdigit() else None,
        is_error=raw.is_error,
    )
