"""Rate limiter, circuit breaker, budget guard and cache. Clocks are injectable for tests."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from opspod.core.errors import LLMUnavailable

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class TokenBucket:
    """Per-provider limiter: queue callers instead of burning 429s; honors Retry-After."""

    def __init__(
        self, per_minute: float, *, clock: Clock = time.monotonic, sleep: Sleep = asyncio.sleep
    ) -> None:
        self._rate = per_minute / 60.0
        self._cap = max(1.0, per_minute / 6)  # small burst; free tiers dislike spikes
        self._tokens = self._cap
        self._clock, self._sleep = clock, sleep
        self._last = clock()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def penalize(self, seconds: float | None) -> None:
        """Provider said Retry-After: block all callers for that long."""
        self._blocked_until = max(self._blocked_until, self._clock() + (seconds or 1.0))

    async def acquire(self, max_wait: float = 30.0) -> None:
        async with self._lock:
            now = self._clock()
            self._tokens = min(self._cap, self._tokens + (now - self._last) * self._rate)
            self._last = now
            wait = max(0.0, self._blocked_until - now)
            if self._tokens < 1.0:
                wait = max(wait, (1.0 - self._tokens) / self._rate)
            if wait > max_wait:
                raise LLMUnavailable(f"rate limit wait {wait:.0f}s exceeds {max_wait:.0f}s")
            if wait:
                await self._sleep(wait)
                self._tokens = min(self._cap, self._tokens + wait * self._rate)
                self._last = self._clock()
            self._tokens -= 1.0


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
