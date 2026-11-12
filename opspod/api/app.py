"""FastAPI app. Intake is idempotent and returns 202; processing happens in the worker."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, text

from opspod.api.auth import parse_keys, require_role
from opspod.core.config import get_settings
from opspod.db.models import AuditLog, Case, CaseStep, Decision
from opspod.domain.enums import Role
from opspod.pipeline.deps import Deps
from opspod.pipeline.intake import create_case
from opspod.pipeline.state import CaseIn

Enqueue = Callable[[str], Awaitable[None]]
ANY_ROLE = (Role.ANALYST, Role.APPROVER, Role.ADMIN, Role.AGENT_SERVICE)


INTAKE_ROLE = Depends(require_role(Role.AGENT_SERVICE, Role.ANALYST, Role.ADMIN))
ANY_ROLE_DEP = Depends(require_role(*ANY_ROLE))


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

    return app
