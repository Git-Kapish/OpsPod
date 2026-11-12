"""State machine runner: persist state after every step; resume from the last completed step.

Each step runs in one DB transaction together with its state snapshot, so a crash leaves either
the whole step applied or none of it. Ledger writes are additionally idempotent, so re-running a
step can never pay twice. Failures are recorded and re-raised; the worker retries, then
dead-letters.
"""

import uuid
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opspod.core.errors import OpsPodError
from opspod.db.audit import write_audit
from opspod.db.models import Case, CaseStep
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


async def run_case(deps: Deps, case_id: uuid.UUID) -> CaseState:
    async with deps.sessionmaker() as s:
        state = await _load_state(s, case_id)
    for name, fn in STEPS:
        if name in state.completed:
            continue
        try:
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
            log.error("step_failed", case_id=str(case_id), step=name, error=type(exc).__name__)
            await _record_failure(deps, case_id, name, exc)
            raise
        state = new
    return state
