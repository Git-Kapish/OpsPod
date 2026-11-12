from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspod.llm.gateway import Gateway
from opspod.policy.models import RegionPolicy
from opspod.skills.base import Skill
from opspod.tools.registry import ToolRegistry

SessionMaker = async_sessionmaker[AsyncSession]


@dataclass
class Deps:
    sessionmaker: SessionMaker
    gateway: Gateway
    policies: dict[str, RegionPolicy]
    fetch_context: Skill
    tools: ToolRegistry
    skills: dict[str, Skill]
    llm_tone: bool = False  # extra LLM call per case; off by default to protect free quotas
    # Test/chaos hook: called with the step name just before it runs; may raise to simulate a crash.
    before_step: Callable[[str], None] | None = None
    extras: dict[str, Any] = field(default_factory=dict)
