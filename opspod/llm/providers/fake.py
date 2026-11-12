"""FakeProvider: scripted responses for tests/CI. Never touches the network."""

from collections.abc import Callable
from dataclasses import dataclass, field

from opspod.core.errors import LLMError
from opspod.llm.types import RawResponse

Script = list[str | Exception] | Callable[[str, str], str]


@dataclass
class FakeProvider:
    """script: a queue of replies/exceptions (consumed in order) or fn(system, user) -> text."""

    script: Script = field(default_factory=list)
    name: str = "fake"
    calls: list[dict[str, str]] = field(default_factory=list)

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, object] | None,
        temperature: float,
        timeout: float,
    ) -> RawResponse:
        self.calls.append({"model": model, "system": system, "user": user})
        if callable(self.script):
            return RawResponse(self.script(system, user), 10, 10)
        if not self.script:
            raise LLMError("FakeProvider script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return RawResponse(item, 10, 10)
