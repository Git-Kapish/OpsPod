"""Human decisions: approve / reject / edit an approval, and reverse an executed action.

Approver actions run through the same tool registry as the agent (scopes + caps apply), with the
same idempotency key, so a double-click or a race with a retry can never pay twice.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspod.core.errors import OpsPodError
from opspod.db.audit import write_audit
from opspod.db.models import Action, Approval, Case, Decision
from opspod.domain.enums import MONEY_OUTCOMES, Outcome
from opspod.ledger.service import net_paid
from opspod.pipeline.deps import Deps
from opspod.pipeline.state import CaseIn
from opspod.skills.redact_pii import rehydrate
from opspod.tools.registry import ToolContext, approver_principal


class ApprovalNotFound(OpsPodError):
    pass


class ApprovalConflict(OpsPodError):
    """Already decided (or otherwise not pending)."""


class ApprovalError(OpsPodError):
    """Invalid human input, e.g. an edited amount above the proposal."""


async def _send_message(
    deps: Deps, s: AsyncSession, case: Case, outcome: Outcome, amount: int
) -> None:
    inp = CaseIn.model_validate(case.payload_json)
    policy = deps.policies[case.region]
    name = (await deps.fetch_context({"external_id": inp.external_id})).rider_name
    out = await deps.skills["draft_customer_message"](
        {
            "outcome": outcome,
            "amount": amount,
            "currency": policy.currency,
            "rider_name": "[RIDER_1]",
            "reference": inp.external_id,
            "sla_hours": policy.sla_hours,
        }
    )
    sent = rehydrate(out.body, {"[RIDER_1]": name} if name else {})
    outbox = deps.extras.get("outbox")
    if isinstance(outbox, list):
        outbox.append({"case_id": str(case.id), "body": sent})
    await write_audit(
        s,
        case_id=case.id,
        event="customer.message",
        actor="opspod-agent",
        payload={"body_tokenized": out.body},
    )


async def decide_approval(
    deps: Deps,
    approval_id: uuid.UUID,
    *,
    actor: str,
    decision: Literal["approve", "reject"],
    edited_amount: int | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    async with deps.sessionmaker() as s, s.begin():
        appr = (
            await s.execute(select(Approval).where(Approval.id == approval_id).with_for_update())
        ).scalar_one_or_none()
        if appr is None:
            raise ApprovalNotFound(str(approval_id))
        if appr.status != "pending":
            raise ApprovalConflict(f"approval already {appr.status}")
        dec = await s.get(Decision, appr.decision_id)
        case = await s.get(Case, appr.case_id)
        assert dec is not None and case is not None
        appr.decided_by, appr.decided_at, appr.note = actor, datetime.now(UTC), note
        action_id: str | None = None

        if decision == "reject":
            appr.status, case.status = "rejected", "rejected"
        else:
            amount = dec.amount
            if edited_amount is not None:
                if not 0 < edited_amount <= dec.amount:
                    raise ApprovalError("edited amount must be positive and not above the proposal")
                amount = edited_amount
                appr.edited_amount = edited_amount
            outcome = Outcome(dec.outcome)
            if outcome in MONEY_OUTCOMES:
                inp = CaseIn.model_validate(case.payload_json)
                ctx = await deps.fetch_context({"external_id": inp.external_id})
                if await net_paid(s, case_id=case.id) + amount > ctx.total:
                    raise ApprovalError("amount exceeds what is still refundable")
                policy = deps.policies[case.region]
                tool = "issue_credit" if outcome is Outcome.CREDIT else "issue_refund"
                res = await deps.tools.call(
                    tool,
                    approver_principal(policy, actor),
                    {
                        "case_id": str(case.id),
                        "version": 1,
                        "account": f"rider:{inp.rider_ref}",
                        "amount": amount,
                        "currency": policy.currency,
                    },
                    ToolContext(s, case.region),
                )
                action_id = res["action_id"]
            appr.status = "edited" if edited_amount is not None else "approved"
            case.status = "resolved"
            await _send_message(deps, s, case, outcome, amount)
        await write_audit(
            s,
            case_id=case.id,
            event="approval.decided",
            actor=actor,
            payload={
                "decision": decision,
                "status": appr.status,
                "edited_amount": edited_amount,
                "note": note,
                "action_id": action_id,
                "approval_id": str(approval_id),
            },
        )
        return {"approval_id": str(approval_id), "status": appr.status, "action_id": action_id}


async def reverse_executed_action(
    deps: Deps, action_id: uuid.UUID, *, actor: str
) -> dict[str, Any]:
    async with deps.sessionmaker() as s, s.begin():
        action = await s.get(Action, action_id)
        if action is None:
            raise ApprovalNotFound(str(action_id))
        case = await s.get(Case, action.case_id)
        assert case is not None
        res: dict[str, Any] = await deps.tools.call(
            "reverse_refund",
            approver_principal(deps.policies[case.region], actor),
            {"action_id": str(action_id)},
            ToolContext(s, case.region),
        )
        if not res["replayed"]:
            case.status = "reversed"
            await write_audit(
                s,
                case_id=case.id,
                event="action.reversed",
                actor=actor,
                payload={"original_action_id": str(action_id), **res},
            )
        return res
