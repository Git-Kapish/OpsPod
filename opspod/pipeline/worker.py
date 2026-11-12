"""arq worker: retries with backoff, then dead-letters (and the case goes to humans)."""

import uuid
from typing import Any

from arq import Retry
from arq.connections import RedisSettings
from prometheus_client import start_http_server
from sqlalchemy import select

from opspod.core.config import get_settings
from opspod.core.logging import configure_logging
from opspod.db.audit import write_audit
from opspod.db.models import Case, DeadLetter
from opspod.observability.metrics import DLQ_TOTAL
from opspod.observability.tracing import configure_exporter
from opspod.pipeline.bootstrap import build_deps
from opspod.pipeline.deps import Deps
from opspod.pipeline.runner import CaseLocked, run_case

MAX_TRIES = 3
INCOMPLETE = ("received", "processing")


async def dead_letter(deps: Deps, case_id: uuid.UUID, error: str, attempts: int) -> None:
    """S10: an unrecoverable failure never auto-executes; it lands in the DLQ for a human."""
    DLQ_TOTAL.inc()
    async with deps.sessionmaker() as s, s.begin():
        s.add(DeadLetter(case_id=case_id, step="pipeline", error=error[:300], attempts=attempts))
        case = await s.get(Case, case_id)
        if case is not None:
            case.status = "dead_letter"
        await write_audit(
            s,
            case_id=case_id,
            event="pipeline.dead_letter",
            actor="worker",
            payload={"error": error[:300], "attempts": attempts},
        )


async def process_case(ctx: dict[str, Any], case_id: str) -> str:
    deps: Deps = ctx["deps"]
    try:
        state = await run_case(deps, uuid.UUID(case_id))
    except CaseLocked:
        return "busy"  # a duplicate delivery; the worker holding the lock finishes the case
    except Exception as exc:  # noqa: BLE001 - retry, then DLQ; never swallow silently
        tries = int(ctx.get("job_try", 1))
        if tries >= MAX_TRIES:
            await dead_letter(deps, uuid.UUID(case_id), f"{type(exc).__name__}: {exc}", tries)
            return "dead_letter"
        raise Retry(defer=2**tries) from exc
    return state.final_status


async def requeue_incomplete(ctx: dict[str, Any]) -> int:
    """On startup, re-enqueue cases a crashed worker left unfinished (runner resumes them)."""
    deps: Deps = ctx["deps"]
    async with deps.sessionmaker() as s:
        ids = (await s.execute(select(Case.id).where(Case.status.in_(INCOMPLETE)))).scalars().all()
    for cid in ids:
        await ctx["redis"].enqueue_job("process_case", str(cid), _job_id=f"resume-{cid}")
    return len(ids)


async def startup(ctx: dict[str, Any]) -> None:
    configure_logging()
    settings = get_settings()
    configure_exporter(settings.otel_exporter_otlp_endpoint)
    start_http_server(settings.worker_metrics_port)
    ctx["deps"] = build_deps(settings)
    await requeue_incomplete(ctx)


class WorkerSettings:
    functions = [process_case]
    on_startup = startup
    max_tries = MAX_TRIES
    max_jobs = 8  # each running case holds a lock connection plus a step connection
    job_timeout = 120
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
