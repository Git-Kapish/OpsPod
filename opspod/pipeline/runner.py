"""State machine runner: persist state after every step; resume from the last completed step.

Each step runs in one DB transaction together with its state snapshot, so a crash leaves either
the whole step applied or none of it. Ledger writes are additionally idempotent, so re-running a
step can never pay twice. Failures are recorded and re-raised; the worker retries, then
dead-letters.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opspod.core.errors import OpsPodError
from opspod.db.audit import write_audit
from opspod.db.models import Case, CaseStep
from opspod.observability.metrics import CASE_SECONDS, CASES, PIPELINE_ERRORS, STEP_SECONDS
from opspod.observability.tracing import case_context, span
from opspod.pipeline import steps
from opspod.pipeline.deps import Deps
from opspod.pipeline.state import CaseIn, CaseState

log = structlog.get_logger()

StepFn = Callable[[CaseState, Deps, AsyncSession], Awaitable[CaseState]]
STEPS: list[tuple[str, StepFn]] = [
    ("gather_context", steps.gather_context),
    ("normalize", steps.normalize),
    ("triage", steps.triage),
    ("llm_analysis", steps.llm_analysis),
    ("policy", steps.policy_step),
    ("execute", steps.execute),
    ("notify", steps.notify),
    ("finalize", steps.finalize),
]


async def _load_state(session: AsyncSession, case_id: uuid.UUID) -> CaseState:
    last = (
        await session.execute(
            select(CaseStep)
            .where(CaseStep.case_id == case_id, CaseStep.status == "completed")
            .order_by(CaseStep.finished_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last is not None:
        return CaseState.model_validate(last.state_json)
    case = await session.get(Case, case_id)
    if case is None:
        raise OpsPodError(f"case {case_id} not found")
    inp = CaseIn.model_validate(case.payload_json)
    return CaseState(
        case_id=case_id,
        external_id=inp.external_id,
        region=case.region,
        rider_ref=inp.rider_ref,
        case_type=inp.case_type,
    )


async def _attempt(session: AsyncSession, case_id: uuid.UUID, step: str) -> int:
    n = (
        await session.execute(
            select(func.count())
            .select_from(CaseStep)
            .where(CaseStep.case_id == case_id, CaseStep.step == step)
        )
    ).scalar_one()
    return int(n) + 1


async def _record_failure(deps: Deps, case_id: uuid.UUID, step: str, exc: Exception) -> None:
    async with deps.sessionmaker() as s, s.begin():
        s.add(
            CaseStep(
                case_id=case_id,
                step=step,
                status="failed",
                state_json={"error": type(exc).__name__},
                attempt=await _attempt(s, case_id, step),
                finished_at=func.now(),
            )
        )
        await write_audit(
            s,
            case_id=case_id,
            event="step.failed",
            actor="pipeline",
            payload={"step": step, "error": type(exc).__name__},
        )


class CaseLocked(OpsPodError):
    """Another worker is already processing this case; the duplicate delivery backs off."""


async def run_case(deps: Deps, case_id: uuid.UUID) -> CaseState:
    """Run (or resume) a case. At most one worker runs a case at a time (advisory lock)."""
    key = case_id.int & 0x7FFFFFFFFFFFFFFF  # pg advisory locks take a signed bigint
    async with deps.sessionmaker() as lock_session:
        got = await lock_session.execute(text("select pg_try_advisory_lock(:k)"), {"k": key})
        if not got.scalar_one():
            raise CaseLocked(str(case_id))
        try:
            return await _run_locked(deps, case_id)
        finally:  # explicit: the lock belongs to the pooled connection, not the session
            await lock_session.execute(text("select pg_advisory_unlock(:k)"), {"k": key})


async def _run_locked(deps: Deps, case_id: uuid.UUID) -> CaseState:
    """trace_id == case_id; every step is a span and a metric."""
    started = time.perf_counter()
    structlog.contextvars.bind_contextvars(case_id=str(case_id), trace_id=case_id.hex)
    try:
        with span("case.process", context=case_context(case_id), case_id=str(case_id)) as root:
            async with deps.sessionmaker() as s:
                state = await _load_state(s, case_id)
            for name, fn in STEPS:
                if name in state.completed:
                    continue
                state = await _run_step(deps, case_id, name, fn, state)
            root.set_attribute("status", state.final_status)
            route = state.decision.route.value if state.decision else "none"
            CASES.labels(state.final_status, route, state.path).inc()
            CASE_SECONDS.observe(time.perf_counter() - started)
            return state
    finally:
        structlog.contextvars.unbind_contextvars("case_id", "trace_id")


async def _run_step(
    deps: Deps, case_id: uuid.UUID, name: str, fn: StepFn, state: CaseState
) -> CaseState:
    t0 = time.perf_counter()
    try:
        with span(f"step.{name}", step=name):
            if deps.chaos is not None and (delay := (await deps.chaos.get()).slow_db_ms):
                await asyncio.sleep(delay / 1000)
            if deps.before_step is not None:
                deps.before_step(name)
            async with deps.sessionmaker() as s, s.begin():
                new = await fn(state, deps, s)
                new.completed = [*state.completed, name]
                s.add(
                    CaseStep(
                        case_id=case_id,
                        step=name,
                        status="completed",
                        state_json=new.model_dump(mode="json"),
                        attempt=await _attempt(s, case_id, name),
                        finished_at=func.now(),
                    )
                )
    except Exception as exc:  # noqa: BLE001 - any failure must be recorded, then retried/DLQ'd
        log.error("step_failed", step=name, error=type(exc).__name__)
        PIPELINE_ERRORS.labels(name).inc()
        await _record_failure(deps, case_id, name, exc)
        raise
    STEP_SECONDS.labels(name).observe(time.perf_counter() - t0)
    return new
