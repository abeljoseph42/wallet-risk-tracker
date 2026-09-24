"""Async token-bucket rate limiter.

Used to keep outbound Etherscan calls under the per-second limit for the
configured plan tier. The clock and sleep functions are injectable so tests
can drive time deterministically instead of sleeping in wall-clock time.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class TokenBucketRateLimiter:
    """Allows up to `rate_per_sec` acquisitions per second, refilled continuously."""

    def __init__(
        self,
        rate_per_sec: float,
        *,
        burst: float = 1.0,
        clock: Clock = time.monotonic,
        sleep: Sleeper | None = None,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._rate = rate_per_sec
        # Default burst of 1 spaces calls evenly. A burst equal to the rate lets
        # rate + refill calls land inside one second, which Etherscan rejects.
        self._capacity = burst
        self._tokens = self._capacity
        self._clock = clock
        self._sleep: Sleeper = sleep if sleep is not None else asyncio.sleep
        self._last_refill = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last_refill)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._last_refill = now

    async def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                deficit = 1 - self._tokens
                await self._sleep(deficit / self._rate)
