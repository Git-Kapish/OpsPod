"""Approvals, reversals, replay and admin flags."""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from opspod.api.auth import require_role
from opspod.core.chaos import CHAOS_KEY, Chaos
from opspod.core.errors import LedgerError, PermissionDenied
from opspod.db.audit import write_audit
from opspod.db.flags import (
    AUTONOMY_KEY,
    KILL_KEY,
    Autonomy,
    get_autonomy,
    get_chaos,
    kill_switch_on,
    set_flag,
)
from opspod.db.models import Approval, Case, DeadLetter, Decision
from opspod.domain.enums import Role
from opspod.pipeline.approvals import (
    ApprovalConflict,
    ApprovalError,
    ApprovalNotFound,
    decide_approval,
    reverse_executed_action,
)
from opspod.pipeline.deps import Deps
from opspod.pipeline.replay import replay_case

ANALYST_UP = Depends(require_role(Role.ANALYST, Role.APPROVER, Role.ADMIN))
APPROVER = Depends(require_role(Role.APPROVER, Role.ADMIN))
ADMIN = Depends(require_role(Role.ADMIN))


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    edited_amount: int | None = None
    note: str | None = None


class FlagsIn(BaseModel):
    autonomy: Autonomy | None = None
    kill_switch: bool | None = None


async def list_pending(deps: Deps, status: str = "pending") -> list[dict[str, Any]]:
    async with deps.sessionmaker() as s:
        rows = (
            await s.execute(
                select(Approval, Decision, Case)
                .join(Decision, Decision.id == Approval.decision_id)
                .join(Case, Case.id == Approval.case_id)
                .where(Approval.status == status)
                .order_by(Approval.requested_at)
            )
        ).all()
    return [
        {
            "id": str(a.id),
            "case_id": str(c.id),
            "region": c.region,
            "type": c.type,
            "outcome": d.outcome,
            "amount": d.amount,
            "rules": d.policy_rules_fired,
            "source": d.source,
            "rationale": d.rationale,
            "requested_at": a.requested_at.isoformat(),
        }
        for a, d, c in rows
    ]


def actor_of(request: Request) -> str:
    return str(request.state.actor)


def http_error(exc: Exception) -> HTTPException:
    mapping: dict[type[Exception], int] = {
        ApprovalNotFound: 404,
        ApprovalConflict: 409,
        ApprovalError: 422,
        PermissionDenied: 403,
        LedgerError: 409,
    }
    return HTTPException(mapping.get(type(exc), 400), str(exc))


def register_ops(app: FastAPI, deps: Deps, enqueue: Callable[..., Awaitable[None]]) -> None:
    @app.get("/v1/approvals")
    async def approvals(status: str = "pending", _: Role = ANALYST_UP) -> list[dict[str, Any]]:
        return await list_pending(deps, status)

    @app.post("/v1/approvals/{approval_id}/decision")
    async def decide(
        approval_id: uuid.UUID, body: DecisionIn, request: Request, _: Role = APPROVER
    ) -> dict[str, Any]:
        try:
            return await decide_approval(
                deps,
                approval_id,
                actor=actor_of(request),
                decision=body.decision,
                edited_amount=body.edited_amount,
                note=body.note,
            )
        except (
            ApprovalNotFound,
            ApprovalConflict,
            ApprovalError,
            PermissionDenied,
            LedgerError,
        ) as e:
            raise http_error(e) from e

    @app.post("/v1/actions/{action_id}/reverse")
    async def reverse(action_id: uuid.UUID, request: Request, _: Role = APPROVER) -> dict[str, Any]:
        try:
            return await reverse_executed_action(deps, action_id, actor=actor_of(request))
        except (ApprovalNotFound, PermissionDenied, LedgerError) as e:
            raise http_error(e) from e

    @app.get("/v1/cases/{case_id}/replay")
    async def replay(case_id: uuid.UUID, _: Role = ANALYST_UP) -> dict[str, Any]:
        out = await replay_case(deps, case_id)
        if not out["found"]:
            raise HTTPException(404, "case not found")
        return out

    @app.get("/v1/admin/flags")
    async def get_flags(_: Role = ADMIN) -> dict[str, Any]:
        async with deps.sessionmaker() as s:
            return {
                "autonomy": (await get_autonomy(s)).model_dump(mode="json"),
                "kill_switch": await kill_switch_on(s),
            }

    @app.put("/v1/admin/flags")
    async def put_flags(body: FlagsIn, request: Request, _: Role = ADMIN) -> dict[str, Any]:
        async with deps.sessionmaker() as s, s.begin():
            if body.autonomy is not None:
                await set_flag(
                    s, AUTONOMY_KEY, body.autonomy.model_dump(mode="json"), actor_of(request)
                )
            if body.kill_switch is not None:
                await set_flag(s, KILL_KEY, {"enabled": body.kill_switch}, actor_of(request))
            await write_audit(
                s,
                case_id=None,
                event="flags.changed",
                actor=actor_of(request),
                payload=body.model_dump(mode="json", exclude_none=True),
            )
        async with deps.sessionmaker() as s:
            return {
                "autonomy": (await get_autonomy(s)).model_dump(mode="json"),
                "kill_switch": await kill_switch_on(s),
            }

    @app.get("/v1/admin/chaos")
    async def get_chaos_flags(_: Role = ADMIN) -> dict[str, Any]:
        async with deps.sessionmaker() as s:
            return (await get_chaos(s)).model_dump()

    @app.post("/v1/admin/chaos")
    async def set_chaos_flags(body: Chaos, request: Request, _: Role = ADMIN) -> dict[str, Any]:
        if request.app.state.app_env != "dev":
            raise HTTPException(404, "not found")  # failure injection is dev-only
        async with deps.sessionmaker() as s, s.begin():
            await set_flag(s, CHAOS_KEY, body.model_dump(), actor_of(request))
            await write_audit(
                s,
                case_id=None,
                event="chaos.changed",
                actor=actor_of(request),
                payload=body.model_dump(),
            )
        return body.model_dump()

    @app.post("/v1/admin/dlq/{dl_id}/retry")
    async def retry_dead_letter(
        dl_id: uuid.UUID, request: Request, _: Role = APPROVER
    ) -> dict[str, Any]:
        """After fixing the cause: resolve the dead letter, re-run from the last step."""
        async with deps.sessionmaker() as s, s.begin():
            dl = await s.get(DeadLetter, dl_id)
            if dl is None:
                raise HTTPException(404, "dead letter not found")
            if dl.resolved_at is not None:
                raise HTTPException(409, "dead letter already resolved")
            dl.resolved_at, dl.resolved_by = datetime.now(UTC), actor_of(request)
            case = await s.get(Case, dl.case_id)
            if case is not None:
                case.status = "processing"
            await write_audit(
                s,
                case_id=dl.case_id,
                event="dlq.retry",
                actor=actor_of(request),
                payload={"dead_letter_id": str(dl_id)},
            )
            case_id = dl.case_id
        await enqueue(str(case_id), force=True)  # fresh job id; the runner resumes from state
        return {"case_id": str(case_id), "requeued": True}
