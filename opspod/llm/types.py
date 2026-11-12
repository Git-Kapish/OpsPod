from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class RawResponse:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


class Provider(Protocol):
    name: str

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, object] | None,
        temperature: float,
        timeout: float,
    ) -> RawResponse: ...


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    parsed: T
    raw_text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_s: float
    attempts: int  # provider calls made for this result, repair included
    repaired: bool
    cached: bool
    fallback_used: bool  # served by a non-primary model in the task's chain
