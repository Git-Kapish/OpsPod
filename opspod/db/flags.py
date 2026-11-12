"""Runtime flags in the `flags` table: autonomy mode (per region/case type), canary %, kill switch.

Safe defaults when nothing is set: ASSISTED (human approves everything), kill switch off.
"""

import time

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opspod.core.chaos import CHAOS_KEY, Chaos
from opspod.db.models import Flag
from opspod.domain.enums import AutonomyMode

AUTONOMY_KEY, KILL_KEY = "autonomy", "kill_switch"


class Autonomy(BaseModel):
    default: AutonomyMode = AutonomyMode.ASSISTED
    # keys: "us:wrong_fare", "us", or "*:wrong_fare"; most specific wins
    overrides: dict[str, AutonomyMode] = {}
    canary_percent: int = Field(default=100, ge=0, le=100)

    def resolve(self, region: str, case_type: str | None) -> AutonomyMode:
        for key in (f"{region}:{case_type}", region, f"*:{case_type}"):
            if key in self.overrides:
                return self.overrides[key]
        return self.default


async def get_autonomy(session: AsyncSession) -> Autonomy:
    row = (await session.execute(select(Flag).where(Flag.key == AUTONOMY_KEY))).scalar_one_or_none()
    return Autonomy.model_validate(row.value_json) if row else Autonomy()


async def kill_switch_on(session: AsyncSession) -> bool:
    row = (await session.execute(select(Flag).where(Flag.key == KILL_KEY))).scalar_one_or_none()
    return bool(row and row.value_json.get("enabled"))


async def set_flag(session: AsyncSession, key: str, value: dict[str, object], by: str) -> None:
    stmt = insert(Flag).values(key=key, value_json=value, updated_by=by)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Flag.key], set_={"value_json": value, "updated_by": by}
    )
    await session.execute(stmt)


async def get_chaos(session: AsyncSession) -> Chaos:
    row = (await session.execute(select(Flag).where(Flag.key == CHAOS_KEY))).scalar_one_or_none()
    return Chaos.model_validate(row.value_json) if row else Chaos()


class DbChaos:
    """Reads chaos flags from the DB with a short TTL so API and worker processes agree."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], ttl_s: float = 1.0) -> None:
        self._sm, self._ttl = sessionmaker, ttl_s
        self._cached: tuple[float, Chaos] | None = None

    async def get(self) -> Chaos:
        now = time.monotonic()
        if self._cached is None or now - self._cached[0] > self._ttl:
            async with self._sm() as s:
                self._cached = (now, await get_chaos(s))
        return self._cached[1]
