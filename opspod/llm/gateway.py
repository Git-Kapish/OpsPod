"""LLM gateway: routing, fallbacks, retries, repair, rate limiting, breaker, budget, cache.

Fail-safe contract (S10): this either returns a schema-valid result or raises LLMUnavailable /
LLMInvalidOutput. Callers (the pipeline) turn any raise into a human route; never an action.
"""

import asyncio
import json
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from pydantic import ValidationError

from opspod.core.errors import (
    LLMError,
    LLMInvalidOutput,
    LLMRateLimited,
    LLMTimeout,
    LLMUnavailable,
)
from opspod.llm.config import Candidate, LLMConfig, ProviderLimits
from opspod.llm.providers.cassette import request_key
from opspod.llm.resilience import (
    BudgetGuard,
    CircuitBreaker,
    Clock,
    LRUCache,
    Sleep,
    TokenBucket,
)
from opspod.llm.types import LLMResult, Provider, T


@dataclass(frozen=True)
class CallEvent:
    """One provider attempt; Phase 6 turns these into spans and metrics."""

    task: str
    provider: str
    model: str
    outcome: str  # ok|rate_limited|timeout|error|invalid|breaker_open|budget|cache_hit
    latency_s: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    repaired: bool = False


def extract_json(text: str) -> str:
    """Strip code fences / chatter around the first JSON object."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


class Gateway:
    def __init__(
        self,
        config: LLMConfig,
        providers: dict[str, Provider],
        *,
        on_event: Callable[[CallEvent], None] | None = None,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._cfg, self._providers = config, providers
        self._on_event = on_event or (lambda _e: None)
        self._clock, self._sleep = clock, sleep
        self._rng = rng or random.Random()  # noqa: S311 - backoff jitter only
        self._cache = LRUCache()
        self._breakers: dict[str, CircuitBreaker] = {}
        self._limiters: dict[str, TokenBucket] = {}
        self._budgets: dict[str, BudgetGuard] = {}
        for name in providers:
            lim = config.limits.get(name, ProviderLimits())
            self._breakers[name] = CircuitBreaker(
                config.breaker_threshold, config.breaker_reset_s, clock=clock
            )
            self._limiters[name] = TokenBucket(lim.requests_per_minute, clock=clock, sleep=sleep)
            self._budgets[name] = BudgetGuard(lim.daily_calls, lim.daily_tokens)

    def budget_near_limit(self) -> bool:
        return any(b.near_limit for b in self._budgets.values())

    async def generate_structured(
        self,
        *,
        task: str,
        system: str,
        user: str,
        schema: type[T],
        temperature: float = 0.0,
        max_retries: int = 2,
    ) -> LLMResult[T]:
        chain = self._cfg.tasks.get(task) or []
        if not chain:
            raise LLMUnavailable(f"no models configured for task {task!r}")
        schema_json: dict[str, object] = schema.model_json_schema()
        system_full = (
            f"{system}\n\nRespond with ONLY a JSON object matching this JSON schema:\n"
            f"{json.dumps(schema_json)}"
        )
        failures: list[str] = []
        invalid: LLMInvalidOutput | None = None
        for idx, cand in enumerate(chain):
            try:
                res = await self._try(
                    task, cand, system_full, user, schema, schema_json, temperature, max_retries
                )
            except LLMInvalidOutput as exc:
                invalid = exc
                failures.append(f"{cand.model}: invalid output")
            except LLMError as exc:  # LLMUnavailable, timeouts and rate limits exhausted
                failures.append(f"{cand.model}: {exc}")
            else:
                return replace(res, fallback_used=idx > 0)
        if invalid is not None and len(failures) == 1:
            raise invalid
        raise LLMUnavailable("all providers failed: " + "; ".join(failures))

    def _emit(self, task: str, cand: Candidate, outcome: str, **kw: Any) -> None:
        self._on_event(CallEvent(task, cand.provider, cand.model, outcome, **kw))

    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        wait = self._cfg.backoff_base_s * float(2**attempt) * (0.5 + self._rng.random() / 2)
        return min(max(wait, retry_after or 0.0), self._cfg.max_wait_s)

    async def _try(
        self,
        task: str,
        cand: Candidate,
        system: str,
        user: str,
        schema: type[T],
        schema_json: dict[str, object],
        temperature: float,
        max_retries: int,
    ) -> LLMResult[T]:
        provider = self._providers.get(cand.provider)
        if provider is None:
            raise LLMUnavailable(f"provider {cand.provider} not configured")
        key = request_key(cand.model, system, user, schema_json, temperature)
        hit = self._cache.get(key)
        if isinstance(hit, LLMResult):
            self._emit(task, cand, "cache_hit")
            return replace(hit, cached=True)
        breaker, limiter, budget = (
            self._breakers[cand.provider],
            self._limiters[cand.provider],
            self._budgets[cand.provider],
        )
        if not breaker.allow():
            self._emit(task, cand, "breaker_open")
            raise LLMUnavailable(f"circuit open for {cand.provider}")

        user_msg, repaired, transport_failures, calls = user, False, 0, 0
        while True:
            try:
                budget.check()
                await limiter.acquire(self._cfg.max_wait_s)
            except LLMUnavailable:
                self._emit(task, cand, "budget")
                breaker.record_failure()
                raise
            t0 = self._clock()
            calls += 1
            try:
                raw = await asyncio.wait_for(
                    provider.complete(
                        model=cand.model,
                        system=system,
                        user=user_msg,
                        schema=schema_json if cand.native_schema else None,
                        temperature=temperature,
                        timeout=self._cfg.timeout_s,
                    ),
                    timeout=self._cfg.timeout_s + 5,
                )
            except (LLMRateLimited, LLMTimeout, TimeoutError, LLMError) as exc:
                breaker.record_failure()
                outcome = (
                    "rate_limited"
                    if isinstance(exc, LLMRateLimited)
                    else "timeout"
                    if isinstance(exc, LLMTimeout | TimeoutError)
                    else "error"
                )
                self._emit(task, cand, outcome, latency_s=self._clock() - t0)
                retry_after = exc.retry_after if isinstance(exc, LLMRateLimited) else None
                if isinstance(exc, LLMRateLimited):
                    limiter.penalize(retry_after)
                retryable = outcome != "error" or "provider 5" in str(exc)
                if not retryable or transport_failures >= max_retries or not breaker.allow():
                    if isinstance(exc, TimeoutError):
                        raise LLMTimeout("provider call timed out") from exc
                    raise
                await self._sleep(self._backoff(transport_failures, retry_after))
                transport_failures += 1
                continue
            breaker.record_success()
            budget.record(raw.tokens_in + raw.tokens_out)
            latency = self._clock() - t0
            try:
                parsed = schema.model_validate_json(extract_json(raw.text))
            except (ValidationError, ValueError) as exc:
                self._emit(
                    task,
                    cand,
                    "invalid",
                    latency_s=latency,
                    tokens_in=raw.tokens_in,
                    tokens_out=raw.tokens_out,
                    repaired=repaired,
                )
                if repaired:
                    raise LLMInvalidOutput(f"{cand.model} output invalid after repair") from exc
                repaired = True  # one repair retry: feed the validation error back
                user_msg = (
                    f"{user}\n\nYour previous reply was invalid: {str(exc)[:400]}\n"
                    f"Previous reply:\n{raw.text[:1500]}\nReturn ONLY the corrected JSON."
                )
                continue
            self._emit(
                task,
                cand,
                "ok",
                latency_s=latency,
                tokens_in=raw.tokens_in,
                tokens_out=raw.tokens_out,
                repaired=repaired,
            )
            result = LLMResult(
                parsed=parsed,
                raw_text=raw.text,
                provider=cand.provider,
                model=cand.model,
                tokens_in=raw.tokens_in,
                tokens_out=raw.tokens_out,
                latency_s=latency,
                attempts=calls,
                repaired=repaired,
                cached=False,
                fallback_used=False,
            )
            self._cache.put(key, result)
            return result
