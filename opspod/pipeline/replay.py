"""Replay a case from its audit record only. No side effects, no network, no LLM.

Re-derives each deterministic stage from what was recorded and compares to what happened:
rule triage (from the stored context snapshot), the LLM proposal (re-parsed from the stored raw
output), and the policy decision (re-evaluated from stored facts + proposal).
"""

import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opspod.db.models import AuditLog, Case
from opspod.domain.enums import CaseType
from opspod.pipeline.deps import Deps
from opspod.pipeline.schemas import AnalysisOutput
from opspod.pipeline.state import CaseIn
from opspod.policy.engine import evaluate
from opspod.policy.models import CaseFacts, PolicyDecision, Proposal


async def _events(s: AsyncSession, case_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    rows = (
        await s.execute(select(AuditLog).where(AuditLog.case_id == case_id).order_by(AuditLog.id))
    ).scalars()
    return {r.event: r.payload_json for r in rows}  # later events of the same kind win


async def replay_case(deps: Deps, case_id: uuid.UUID) -> dict[str, Any]:
    async with deps.sessionmaker() as s:
        case = await s.get(Case, case_id)
        if case is None:
            return {"found": False}
        ev = await _events(s, case_id)
    diffs: list[str] = []
    inp = CaseIn.model_validate(case.payload_json)
    pol_rec = ev.get("policy.decision")
    if pol_rec is None:
        return {"found": True, "identical": False, "diffs": ["no policy.decision recorded"]}

    # 1. rule triage from the recorded context snapshot
    ctx, triage = ev.get("context.gathered", {}).get("context"), ev.get("triage.done")
    if ctx and triage and triage.get("calc") and inp.case_type and ctx.get("payments"):
        dup = 0
        if inp.case_type is CaseType.DUPLICATE_CHARGE:
            pays = [
                {"id": str(i), "amount": a, "minutes_since_first": 2 * i}
                for i, a in enumerate(ctx["payments"])
            ]
            dup = (
                await deps.skills["detect_duplicate_charge"]({"payments": pays})
            ).duplicate_amount
        calc = await deps.skills["calculate_refund"](
            {
                "case_type": inp.case_type,
                "total": ctx["total"],
                "facts": ctx["fixtures"],
                "duplicate_amount": dup,
            }
        )
        if (calc.outcome.value, calc.amount) != (
            triage["calc"]["outcome"],
            triage["calc"]["amount"],
        ):
            diffs.append("rule calculation differs from recorded")

    # 2. LLM proposal re-parsed from stored raw output
    llm = ev.get("llm.proposal")
    if llm is not None:
        try:
            parsed = AnalysisOutput.model_validate_json(llm["raw_output"])
            if parsed.model_dump(mode="json") != llm["parsed"]:
                diffs.append("re-parsed LLM output differs from recorded parse")
        except ValidationError:
            diffs.append("recorded LLM output no longer parses")

    # 3. policy decision from recorded facts + proposal
    policy = deps.policies[case.region]
    if policy.version != pol_rec["policy_version"]:
        diffs.append(f"policy version changed: {pol_rec['policy_version']} -> {policy.version}")
    again = evaluate(
        policy,
        Proposal.model_validate(pol_rec["proposal"]),
        CaseFacts.model_validate(pol_rec["facts"]),
    )
    if again != PolicyDecision.model_validate(pol_rec["decision"]):
        diffs.append("policy decision differs from recorded")
    return {
        "found": True,
        "identical": not diffs,
        "diffs": diffs,
        "recorded": pol_rec["decision"],
        "replayed": again.model_dump(mode="json"),
    }
