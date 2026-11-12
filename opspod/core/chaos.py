"""Failure injection flags (PRD 10.4). Off by default; admin endpoint is dev-only.

Expected behavior per flag is documented in docs/runbook.md and enforced by tests/chaos.
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

CHAOS_KEY = "chaos"


class Chaos(BaseModel):
    model_config = ConfigDict(extra="forbid")
    llm_timeout: bool = False  # provider calls time out
    llm_429: bool = False  # provider calls are rate limited
    llm_malformed: bool = False  # provider returns non-JSON
    tool_500: bool = False  # money/tool layer raises an unexpected error
    llm_provider: str | None = None  # scope llm_* flags to one provider (default: all)
    slow_db_ms: int = Field(default=0, ge=0, le=60_000)  # added latency before every step
    duplicate_webhook: bool = False  # intake delivers each case to the queue 3 times
    poisoned_text: bool = False  # intake appends an injection payload to complaint text

    @property
    def any_on(self) -> bool:
        return any(v for k, v in self.model_dump().items() if k != "llm_provider")


class ChaosSource(Protocol):
    async def get(self) -> Chaos: ...


class StaticChaos:
    """In-process chaos for tests: mutate `.state` between calls."""

    def __init__(self, state: Chaos | None = None) -> None:
        self.state = state or Chaos()

    async def get(self) -> Chaos:
        return self.state


class ChaosError(RuntimeError):
    """Injected failure. Deliberately not an OpsPodError so it takes the retry/DLQ path."""
