"""FastAPI app. Intake is idempotent and returns 202; processing happens in the worker."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import structlog
from fastapi import Depends, FastAPI, Header, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel
from sqlalchemy import func, select, text

from opspod.api.auth import parse_keys, require_role
from opspod.api.ops import register_ops
from opspod.console.routes import register_console
from opspod.core.config import get_settings
from opspod.db.models import Approval, AuditLog, Case, CaseStep, DeadLetter, Decision
from opspod.domain.enums import Role
from opspod.observability.metrics import (
    APPROVAL_QUEUE,
    DLQ_DEPTH,
    INTAKE,
    LLM_BUDGET_NEAR,
)
from opspod.pipeline.deps import Deps
from opspod.pipeline.intake import create_case
from opspod.pipeline.state import CaseIn

log = structlog.get_logger()
Enqueue = Callable[[str], Awaitable[None]]
ANY_ROLE = (Role.ANALYST, Role.APPROVER, Role.ADMIN, Role.AGENT_SERVICE)


INTAKE_ROLE = Depends(require_role(Role.AGENT_SERVICE, Role.ANALYST, Role.ADMIN))
ANY_ROLE_DEP = Depends(require_role(*ANY_ROLE))


ADMIN_ROLE = Depends(require_role(Role.ADMIN))


class CaseAccepted(BaseModel):
    case_id: uuid.UUID
    created: bool


def create_app(deps: Deps, enqueue: Enqueue) -> FastAPI:
    app = FastAPI(title="OpsPod", version="0.1.0")
    app.state.deps = deps
    app.state.api_keys = parse_keys(get_settings().api_keys)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics() -> Response:
        async with deps.sessionmaker() as s:
            pending = await s.scalar(
                select(func.count()).select_from(Approval).where(Approval.status == "pending")
            )
            dlq = await s.scalar(
                select(func.count()).select_from(DeadLetter).where(DeadLetter.resolved_at.is_(None))
            )
        APPROVAL_QUEUE.set(pending or 0)
        DLQ_DEPTH.set(dlq or 0)
        LLM_BUDGET_NEAR.set(1 if deps.gateway.budget_near_limit() else 0)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/internal/alerts", status_code=204)
    async def alerts(payload: dict[str, Any], _: Role = ADMIN_ROLE) -> None:
        """Alertmanager webhook: logs firing alerts (local stand-in for paging)."""
        for a in payload.get("alerts", []):
            labels = a.get("labels", {})
            log.warning(
                "alert",
                status=a.get("status"),
                alertname=labels.get("alertname"),
                severity=labels.get("severity"),
            )

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        try:
            async with deps.sessionmaker() as s:
                await s.execute(text("select 1"))
        except Exception as exc:  # noqa: BLE001 - readiness must report, not crash
            raise HTTPException(503, f"db unavailable: {type(exc).__name__}") from exc
        return {"status": "ready"}

    @app.post("/v1/cases", status_code=202)
    async def post_case(
        payload: CaseIn,
        idempotency_key: Annotated[str | None, Header()] = None,
        _: Role = INTAKE_ROLE,
    ) -> CaseAccepted:
        if not idempotency_key:
            raise HTTPException(400, "Idempotency-Key header required")
        if payload.region not in deps.policies:
            raise HTTPException(422, f"unsupported region {payload.region!r}")
        async with deps.sessionmaker() as s, s.begin():
            case_id, created = await create_case(s, payload, idempotency_key)
        await enqueue(str(case_id))  # job id == case id, so duplicates collapse in the queue
        INTAKE.labels("created" if created else "duplicate").inc()
        return CaseAccepted(case_id=case_id, created=created)

    @app.get("/v1/cases/{case_id}")
    async def get_case(case_id: uuid.UUID, _: Role = ANY_ROLE_DEP) -> dict[str, Any]:
        async with deps.sessionmaker() as s:
            case = await s.get(Case, case_id)
            if case is None:
                raise HTTPException(404, "case not found")
            decisions = (
                (await s.execute(select(Decision).where(Decision.case_id == case_id)))
                .scalars()
                .all()
            )
            steps = (
                (
                    await s.execute(
                        select(CaseStep)
                        .where(CaseStep.case_id == case_id)
                        .order_by(CaseStep.started_at)
                    )
                )
                .scalars()
                .all()
            )
            audit = (
                await s.execute(
                    select(AuditLog.event, AuditLog.created_at)
                    .where(AuditLog.case_id == case_id)
                    .order_by(AuditLog.id)
                )
            ).all()
        return {
            "id": str(case.id),
            "external_id": case.external_id,
            "type": case.type,
            "region": case.region,
            "status": case.status,
            "decisions": [
                {
                    "source": d.source,
                    "outcome": d.outcome,
                    "amount": d.amount,
                    "route": d.route,
                    "rules": d.policy_rules_fired,
                    "rationale": d.rationale,
                }
                for d in decisions
            ],
            "timeline": [
                {"step": st.step, "status": st.status, "attempt": st.attempt} for st in steps
            ],
            "events": [{"event": e, "at": t.isoformat()} for e, t in audit],
        }

    register_ops(app, deps)
    register_console(app, deps)
    return app
