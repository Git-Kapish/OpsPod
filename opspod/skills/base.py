"""Skill = typed, versioned capability with validated input/output contracts."""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from opspod.observability.tracing import span


@dataclass(frozen=True)
class Skill:
    name: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    fn: Callable[[Any], Any]

    async def __call__(self, raw: BaseModel | dict[str, Any]) -> Any:
        with span(f"skill.{self.name}", skill_version=self.version):
            inp = self.input_model.model_validate(
                raw if isinstance(raw, dict) else raw.model_dump()
            )
            out = self.fn(inp)
            if inspect.isawaitable(out):
                out = await out
            return self.output_model.model_validate(
                out if isinstance(out, dict) else out.model_dump()
            )
