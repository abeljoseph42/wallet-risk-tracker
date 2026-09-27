"""Retry an HTTP call on transport errors, 429 and 5xx, with exponential backoff."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

Sleeper = Callable[[float], Awaitable[None]]


class RetryError(Exception):
    """The call still failed after every retry (or failed with a non-retryable status)."""


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    backoff_base_seconds: float = 0.5
    sleep: Sleeper = field(default=asyncio.sleep)


async def send_with_retry(
    send: Callable[[], Awaitable[httpx.Response]], *, what: str, policy: RetryPolicy
) -> httpx.Response:
    last_error = ""
    for attempt in range(1, policy.max_retries + 1):
        try:
            response = await send()
        except httpx.TransportError as exc:
            last_error = f"transport error: {type(exc).__name__}"
        else:
            if response.status_code < 400:
                return response
            if response.status_code != 429 and response.status_code < 500:
                raise RetryError(f"{what} returned HTTP {response.status_code}")
            last_error = f"HTTP {response.status_code}"
        if attempt < policy.max_retries:
            delay = policy.backoff_base_seconds * 2 ** (attempt - 1)
            logger.warning("%s failed (%s); retrying in %.2fs", what, last_error, delay)
            await policy.sleep(delay)
    raise RetryError(f"{what} failed after {policy.max_retries} attempts: {last_error}")
