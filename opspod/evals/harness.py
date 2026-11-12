"""Run golden cases through the real pipeline and collect results.

Uses a fresh `opspod_eval` database per run so frequency caps and day spend start clean.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from opspod.core.config import LLMMode, get_settings
from opspod.db.flags import Autonomy, set_flag
from opspod.db.models import AuditLog
from opspod.db.session import make_sessionmaker
from opspod.domain.enums import AutonomyMode
from opspod.evals.metrics import CaseResult
from opspod.evals.stubs import STUBS
from opspod.ledger.service import net_paid
from opspod.llm.config import load_llm_config
from opspod.llm.factory import build_gateway
from opspod.llm.gateway import CallEvent
from opspod.llm.providers.fake import FakeProvider
from opspod.pipeline.deps import Deps
from opspod.pipeline.intake import create_case
from opspod.pipeline.runner import run_case
from opspod.pipeline.state import CaseIn
from opspod.policy.loader import load_all
from opspod.skills import STATIC_SKILLS
from opspod.skills.fetch_trip_context import FixtureRepo, make_skill
from opspod.tools.registry import default_registry

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ROOT / "evals" / "datasets"
ROUTE_LABEL = {"AUTO_EXECUTE": "auto", "NEEDS_APPROVAL": "approval", "ESCALATE": "escalate"}
PSEUDO_TOOLS = {"create_approval", "escalate_case"}


def load_cases(split: str = "dev") -> list[dict[str, Any]]:
    rows = [
        json.loads(line)
        for f in sorted(DATASETS.glob("*.jsonl"))
        for line in f.read_text(encoding="utf-8").splitlines()
        if line
    ]
    return rows if split == "all" else [c for c in rows if c["split"] == split]


async def prepare_db() -> str:
    base = get_settings().database_url
    url = base.rsplit("/", 1)[0] + "/opspod_eval"
    admin = create_async_engine(base, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as c:
        exists = (
            await c.execute(text("select 1 from pg_database where datname='opspod_eval'"))
        ).scalar()
        if exists is None:
            await c.execute(text("create database opspod_eval"))
    await admin.dispose()
    eng = create_async_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with eng.connect() as c:
        await c.execute(text("drop schema public cascade"))
        await c.execute(text("create schema public"))
    await eng.dispose()
    return url


def migrate(url: str) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "opspod" / "db" / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")


def build_eval_deps(url: str, llm: str, stub: str, events: list[dict[str, Any]]) -> Deps:
    settings = get_settings()

    def sink(e: CallEvent) -> None:
        events.append({"task": e.task, "provider": e.provider, "outcome": e.outcome})

    if llm == "stub":
        gateway = build_gateway(
            LLMMode.FAKE, load_llm_config(), settings, fake=FakeProvider(STUBS[stub]), on_event=sink
        )
    elif llm == "replay":
        gateway = build_gateway(LLMMode.REPLAY, load_llm_config(), settings, on_event=sink)
    else:
        gateway = build_gateway(
            LLMMode.LIVE, load_llm_config(), settings, record_cassettes=True, on_event=sink
        )
    return Deps(
        sessionmaker=make_sessionmaker(url),
        gateway=gateway,
        policies=load_all(),
        fetch_context=make_skill(FixtureRepo(load_cases("all"))),
        tools=default_registry(),
        skills=STATIC_SKILLS,
    )


async def _actual_tools(deps: Deps, case_id: uuid.UUID) -> list[str]:
    async with deps.sessionmaker() as s:
        rows = (
            await s.execute(
                select(AuditLog.event, AuditLog.payload_json).where(AuditLog.case_id == case_id)
            )
        ).all()
    tools: list[str] = []
    for event, payload in rows:
        if event == "action.executed":
            tools.append(payload["tool"])
        elif event == "approval.requested":
            tools.append("create_approval")
        elif event == "case.escalated":
            tools.append("escalate_case")
    return tools


async def run_eval(
    cases: list[dict[str, Any]],
    llm: str = "stub",
    stub: str = "good",
    mode: AutonomyMode = AutonomyMode.AUTO_LOW_RISK,
    canary: int = 100,
) -> tuple[list[CaseResult], list[dict[str, Any]]]:
    url = await prepare_db()
    await asyncio.to_thread(migrate, url)  # alembic env uses asyncio.run
    events: list[dict[str, Any]] = []
    deps = build_eval_deps(url, llm, stub, events)
    async with deps.sessionmaker() as s, s.begin():
        flag = Autonomy(default=mode, canary_percent=canary)
        await set_flag(s, "autonomy", flag.model_dump(mode="json"), "eval")
    run_id = uuid.uuid4().hex[:6]
    known_tools = {t["name"] for t in deps.tools.describe()} | PSEUDO_TOOLS
    results: list[CaseResult] = []
    for case in cases:
        i = case["input"]
        payload = CaseIn(
            external_id=i["external_id"],
            case_type=case["case_type"],
            region=case["region"],
            rider_ref=f"{i['rider_ref']}-{run_id}",
            complaint_text=i["complaint_text"],
        )
        async with deps.sessionmaker() as s, s.begin():
            cid, _ = await create_case(s, payload, f"{case['id']}-{run_id}")
        calls_before, t0 = len(events), time.perf_counter()
        error: str | None = None
        try:
            state = await run_case(deps, cid)
        except Exception as exc:  # noqa: BLE001 - a crashing case is a result, not an abort
            state, error = None, type(exc).__name__
        latency = time.perf_counter() - t0
        tools = await _actual_tools(deps, cid)
        async with deps.sessionmaker() as s:
            paid = await net_paid(s, case_id=cid)
        d = state.decision if state else None
        results.append(
            CaseResult(
                id=case["id"],
                tags=case["tags"],
                region=case["region"],
                expected=case["expected"],
                actual={
                    "route": ROUTE_LABEL[d.route.value] if d else "error",
                    "outcome": d.outcome.value if d else "ERROR",
                    "amount": d.amount if d else 0,
                    "tools": tools,
                },
                paid=paid,
                auto_limit=deps.policies[case["region"]].limits.auto_limit,
                latency_s=latency,
                llm_calls=len(events) - calls_before,
                tokens=sum(m.tokens_in + m.tokens_out for m in state.llm) if state else 0,
                error=error,
                hallucinated_tools=sum(1 for t in tools if t not in known_tools),
            )
        )
    return results, events
