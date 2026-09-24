"""Tests for TokenBucketRateLimiter using a fake clock (no wall-clock sleeps)."""

import pytest

from app.core.rate_limiter import TokenBucketRateLimiter


class FakeClock:
    """A controllable clock whose `sleep` advances time instead of waiting."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept_for: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept_for.append(seconds)
        self.now += seconds


def _limiter(
    rate: float, *, burst: float | None = None
) -> tuple[TokenBucketRateLimiter, FakeClock]:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(rate, burst=burst, clock=clock.time, sleep=clock.sleep)
    return limiter, clock


async def test_burst_up_to_capacity_does_not_sleep() -> None:
    limiter, clock = _limiter(rate=3, burst=3)

    for _ in range(3):
        await limiter.acquire()

    assert clock.slept_for == []


async def test_exceeding_capacity_sleeps_for_the_deficit() -> None:
    limiter, clock = _limiter(rate=3, burst=3)

    for _ in range(3):
        await limiter.acquire()
    await limiter.acquire()

    assert clock.slept_for == [1 / 3]
    assert clock.now == 1 / 3


async def test_tokens_refill_over_elapsed_time() -> None:
    limiter, clock = _limiter(rate=3, burst=3)
    for _ in range(3):
        await limiter.acquire()

    clock.now += 1.0
    await limiter.acquire()

    assert clock.slept_for == []


async def test_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucketRateLimiter(0)


async def test_default_burst_equals_rate() -> None:
    limiter, clock = _limiter(rate=5)

    for _ in range(5):
        await limiter.acquire()
    assert clock.slept_for == []

    await limiter.acquire()
    assert clock.slept_for == [1 / 5]
