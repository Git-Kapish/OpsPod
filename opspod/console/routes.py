"""Minimal ops console: Jinja2 + HTMX. The value is in the backend; this is just a window on it."""

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.exceptions import HTTPException as StarletteHTTPException

from opspod.api.auth import require_role
from opspod.api.ops import http_error, list_pending
from opspod.console.stats import dashboard_stats
from opspod.core.errors import LedgerError, PermissionDenied
from opspod.db.models import Action, AuditLog, Case, CaseStep, DeadLetter, Decision
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

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
VIEW = Depends(require_role(Role.ANALYST, Role.APPROVER, Role.ADMIN))
APPROVER = Depends(require_role(Role.APPROVER, Role.ADMIN))
HUMAN_ERRORS = (ApprovalNotFound, ApprovalConflict, ApprovalError, PermissionDenied, LedgerError)


def register_console(app: FastAPI, deps: Deps) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def to_login(request: Request, exc: StarletteHTTPException) -> Response:
        path = request.url.path
        if path.startswith("/console") and exc.status_code == 401:
            return RedirectResponse("/console/login", status_code=303)
        return await http_exception_handler(request, exc)

    @app.get("/console/login")
    async def login_form(request: Request) -> Response:
        return TEMPLATES.TemplateResponse(request, "login.html", {})

    @app.post("/console/login")
    async def login(api_key: Annotated[str, Form()], role: Annotated[str, Form()]) -> Response:
        resp = RedirectResponse("/console/queue", status_code=303)
        for name, value in (("opspod_key", api_key), ("opspod_role", role)):
            resp.set_cookie(name, value, httponly=True, samesite="strict")
        return resp

    @app.get("/console/queue")
    async def queue(request: Request, _: Role = VIEW) -> Response:
        ctx = {"rows": await list_pending(deps), "role": request.state.role.value}
        return TEMPLATES.TemplateResponse(request, "queue.html", ctx)

    @app.post("/console/approvals/{approval_id}/decision")
    async def console_decide(
        request: Request,
        approval_id: uuid.UUID,
        decision: Annotated[Literal["approve", "reject"], Form()],
        edited_amount: Annotated[str, Form()] = "",
        note: Annotated[str, Form()] = "",
        _: Role = APPROVER,
    ) -> Response:
        try:
            out = await decide_approval(
                deps,
                approval_id,
                actor=str(request.state.actor),
                decision=decision,
                edited_amount=int(edited_amount) if edited_amount.strip() else None,
                note=note or None,
            )
            msg = str(out["status"])
        except HUMAN_ERRORS as exc:
            msg = f"error: {http_error(exc).detail}"
        except ValueError:
            msg = "error: edited amount must be a whole number of minor units"
        return TEMPLATES.TemplateResponse(request, "_result_row.html", {"msg": msg})

    @app.get("/console/cases/{case_id}")
    async def case_detail(request: Request, case_id: uuid.UUID, _: Role = VIEW) -> Response:
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
            events = (
                (
                    await s.execute(
                        select(AuditLog).where(AuditLog.case_id == case_id).order_by(AuditLog.id)
                    )
                )
                .scalars()
                .all()
            )
            actions = (
                (await s.execute(select(Action).where(Action.case_id == case_id))).scalars().all()
            )
        ctx = {
            "case": case,
            "decisions": decisions,
            "steps": steps,
            "events": events,
            "actions": actions,
            "replay": await replay_case(deps, case_id),
            "role": request.state.role.value,
        }
        return TEMPLATES.TemplateResponse(request, "case.html", ctx)

    @app.post("/console/actions/{action_id}/reverse")
    async def console_reverse(
        request: Request, action_id: uuid.UUID, _: Role = APPROVER
    ) -> Response:
        try:
            await reverse_executed_action(deps, action_id, actor=str(request.state.actor))
        except HUMAN_ERRORS as exc:
            raise http_error(exc) from exc
        async with deps.sessionmaker() as s:
            action = await s.get(Action, action_id)
        target = f"/console/cases/{action.case_id}" if action else "/console/queue"
        return RedirectResponse(target, status_code=303)

    @app.get("/console/dashboard")
    async def dashboard(request: Request, _: Role = VIEW) -> Response:
        return TEMPLATES.TemplateResponse(
            request, "dashboard.html", {"s": await dashboard_stats(deps)}
        )

    @app.get("/console/dlq")
    async def dlq(request: Request, _: Role = VIEW) -> Response:
        async with deps.sessionmaker() as s:
            rows = (
                (await s.execute(select(DeadLetter).order_by(DeadLetter.created_at.desc())))
                .scalars()
                .all()
            )
        return TEMPLATES.TemplateResponse(
            request, "dlq.html", {"rows": rows, "role": request.state.role.value}
        )

    @app.post("/console/dlq/{dl_id}/resolve")
    async def resolve_dlq(request: Request, dl_id: uuid.UUID, _: Role = APPROVER) -> Response:
        async with deps.sessionmaker() as s, s.begin():
            row = await s.get(DeadLetter, dl_id)
            if row is None:
                raise HTTPException(404, "not found")
            row.resolved_at, row.resolved_by = datetime.now(UTC), str(request.state.actor)
        return RedirectResponse("/console/dlq", status_code=303)
