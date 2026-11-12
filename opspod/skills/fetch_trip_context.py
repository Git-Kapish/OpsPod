"""fetch_trip_context: gather case context through a repository (fixtures now, DB later)."""

import json
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel

from opspod.core.errors import OpsPodError
from opspod.skills.base import Skill

DATASETS = Path(__file__).resolve().parents[2] / "evals" / "datasets"


class ContextNotFound(OpsPodError):
    pass


class ContextRepo(Protocol):
    async def get(self, external_id: str) -> dict[str, Any] | None: ...


class FixtureRepo:
    """Serves context from golden-dataset fixtures; used by tests, evals and local runs."""

    def __init__(self, cases: list[dict[str, Any]]) -> None:
        self._by_ext = {c["input"]["external_id"]: c for c in cases}

    @classmethod
    def from_datasets(cls, directory: Path = DATASETS) -> "FixtureRepo":
        rows = [
            json.loads(line)
            for f in sorted(directory.glob("*.jsonl"))
            for line in f.read_text(encoding="utf-8").splitlines()
            if line
        ]
        return cls(rows)

    async def get(self, external_id: str) -> dict[str, Any] | None:
        case = self._by_ext.get(external_id)
        return None if case is None else {"region": case["region"], **case["fixtures"]}


class ContextInput(BaseModel):
    external_id: str


class TripContext(BaseModel):
    region: str
    currency: str
    total: int
    rider_name: str
    prior_disputes: int
    refunds_30d: int
    payments: list[int] | None  # None = payment records missing (REQUEST_INFO territory)
    fixtures: dict[str, Any]

    @property
    def paid_total(self) -> int:
        return sum(self.payments) if self.payments else self.total


def make_skill(repo: ContextRepo) -> Skill:
    async def _fetch(inp: ContextInput) -> TripContext:
        raw = await repo.get(inp.external_id)
        if raw is None:
            raise ContextNotFound(inp.external_id)
        return TripContext(
            region=raw["region"],
            currency=raw["currency"],
            total=raw["total"],
            rider_name=raw.get("rider_name", ""),
            prior_disputes=raw.get("prior_disputes", 0),
            refunds_30d=raw.get("refunds_30d", 0),
            payments=raw.get("payments"),
            fixtures=raw,
        )

    return Skill("fetch_trip_context", "1.0.0", ContextInput, TripContext, _fetch)
