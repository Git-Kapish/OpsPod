"""Rate limiter, circuit breaker, budget guard and cache. Clocks are injectable for tests."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from opspod.core.errors import LLMUnavailable

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class RateLimitWaitExceeded(LLMUnavailable):
    """Shed load: waiting for a rate-limit slot would take longer than the queue-wait bound."""


class TokenBucket:
    """Per-provider limiter that reserves slots instead of holding a lock while sleeping.

    Each caller reserves the next free slot (GCRA) and sleeps outside any lock, so the wait it
    is told is the wait it gets. If that wait exceeds `max_wait` the call is shed immediately
    (the caller falls back or escalates) rather than parking a worker. Honors Retry-After.
    """

    def __init__(
        self, per_minute: float, *, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        self._interval = 60.0 / per_minute
        self._burst = max(1.0, per_minute / 6)  # small burst; free tiers dislike spikes
        self._tat = 0.0  # time the next slot becomes free
        self._blocked_until = 0.0
        self._clock, self._sleep = clock, sleep

    def penalize(self, seconds: float | None) -> None:
        """Provider said Retry-After: block all callers for that long."""
        self._blocked_until = max(self._blocked_until, self._clock() + (seconds or 1.0))

    async def acquire(self, max_wait: float = 5.0) -> float:
        """Reserve a slot; returns seconds waited. Raises RateLimitWaitExceeded if too long."""
        now = self._clock()
        tat = max(self._tat, now)
        wait = max(self._blocked_until - now, tat - now - (self._burst - 1.0) * self._interval, 0.0)
        if wait > max_wait:
            raise RateLimitWaitExceeded(f"rate limit wait {wait:.0f}s exceeds {max_wait:.0f}s")
        self._tat = max(tat, now + wait) + self._interval  # reserve before sleeping
        if wait:
            await self._sleep(wait)
        return wait


class CircuitBreaker:
    """Opens after N consecutive failures; after reset_s allows one half-open probe."""

    def __init__(self, threshold: int = 5, reset_s: float = 30.0, *, clock: Clock = time.monotonic):
        self._threshold, self._reset, self._clock = threshold, reset_s, clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probing = False

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._reset:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self._probing:
            self._probing = True
            return True
        return False

    def release(self) -> None:
        """A half-open probe was abandoned without reaching the provider (shed or over budget)."""
        self._probing = False

    def record_success(self) -> None:
        self._failures, self._opened_at, self._probing = 0, None, False

    def record_failure(self) -> None:
        self._failures += 1
        self._probing = False
        if self._failures >= self._threshold or self._opened_at is not None:
            self._opened_at = self._clock()


class BudgetGuard:
    """Daily call and token ceilings per provider; exhausted -> deterministic + human path."""

    def __init__(
        self, daily_calls: int, daily_tokens: int | None = None, near: float = 0.9
    ) -> None:
        self._calls, self._tokens, self._near = daily_calls, daily_tokens, near
        self._day = self._today()
        self.calls_used = 0
        self.tokens_used = 0

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def _roll(self) -> None:
        if self._today() != self._day:
            self._day, self.calls_used, self.tokens_used = self._today(), 0, 0

    def check(self) -> None:
        self._roll()
        if self.calls_used >= self._calls or (
            self._tokens is not None and self.tokens_used >= self._tokens
        ):
            raise LLMUnavailable("daily LLM budget exhausted")

    def record(self, tokens: int) -> None:
        self._roll()
        self.calls_used += 1
        self.tokens_used += tokens

    @property
    def near_limit(self) -> bool:
        self._roll()
        return self.calls_used >= self._calls * self._near


class LRUCache:
    """Content-hash cache: avoids repeat calls on retries and replays."""

    def __init__(self, size: int = 512) -> None:
        self._size = size
        self._data: OrderedDict[str, object] = OrderedDict()

    def get(self, key: str) -> object | None:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key: str, value: object) -> None:
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._size:
            self._data.popitem(last=False)
