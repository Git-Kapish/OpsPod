"""Pipeline steps. Each is small and typed: (state, deps, session) -> new state.

Rules enforced here: the LLM only produces a Proposal; amounts come from code; every
failure that is not an infrastructure error becomes an ESCALATE proposal (fail-safe, S10);
only the policy engine picks the route; money moves only through the tool registry.
"""

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from opspod.core.errors import LLMError, OpsPodError
from opspod.db.audit import snapshot_hash, write_audit
from opspod.db.flags import get_autonomy, kill_switch_on
from opspod.db.models import Approval, Case, Decision
from opspod.domain.enums import MONEY_OUTCOMES, AutonomyMode, CaseType, Outcome, Route
from opspod.ledger.service import day_spend, net_paid, payout_count
from opspod.observability.metrics import LLM_FALLBACKS, LLM_REPAIRS, POLICY_BLOCKED
from opspod.pipeline.deps import Deps
from opspod.pipeline.schemas import AnalysisOutput, ClassifyOutput, ToneOutput
from opspod.pipeline.state import CalcSnapshot, CaseIn, CaseState, ContextSnapshot, LLMMeta
from opspod.policy.canary import canary_hit
from opspod.policy.engine import evaluate
from opspod.policy.models import CaseFacts, Proposal
from opspod.prompts import Prompt, load_prompt, wrap_untrusted
from opspod.skills.draft_customer_message import MessageOutput
from opspod.skills.fetch_trip_context import ContextNotFound
from opspod.skills.redact_pii import rehydrate
from opspod.tools.registry import ToolContext, agent_principal

AGENT = "opspod-agent"
HEDGES = re.compile(r"(?i)\b(not sure|sort of|kind of|maybe|i think|not certain|unclear|both a)\b")
FRAUD_RULES = {"P005_customer_refund_frequency", "P006_fraud_score", "P009_injection_detected"}


def _rule_escalate(why: str) -> Proposal:
    return Proposal(outcome=Outcome.ESCALATE, confidence=1.0, rationale=why, source="rule")


async def _load_case(session: AsyncSession, state: CaseState) -> tuple[Case, CaseIn]:
    case = await session.get(Case, state.case_id)
    if case is None:
        raise OpsPodError(f"case {state.case_id} not found")
    return case, CaseIn.model_validate(case.payload_json)


async def _effective_mode(session: AsyncSession, state: CaseState) -> tuple[AutonomyMode, int]:
    """Kill switch overrides everything; read fresh so ops can stop the line mid-case."""
    flags = await get_autonomy(session)
    if await kill_switch_on(session):
        return AutonomyMode.KILL_SWITCH, flags.canary_percent
    ctype = state.case_type.value if state.case_type else None
    return flags.resolve(state.region, ctype), flags.canary_percent


# ---- steps -----------------------------------------------------------------------------


async def gather_context(state: CaseState, deps: Deps, session: AsyncSession) -> CaseState:
    state = state.model_copy(deep=True)
    case, inp = await _load_case(session, state)
    case.status = "processing"
    try:
        ctx = await deps.fetch_context({"external_id": inp.external_id})
    except ContextNotFound:
        await write_audit(
            session,
            case_id=state.case_id,
            event="context.missing",
            actor=AGENT,
            payload={"external_id": inp.external_id},
        )
        return state
    fixtures = {k: v for k, v in ctx.fixtures.items() if k != "rider_name"}
    state.context = ContextSnapshot(
        currency=ctx.currency,
        total=ctx.total,
        payments=ctx.payments,
        prior_disputes=ctx.prior_disputes,
        refunds_30d=ctx.refunds_30d,
        fixtures=fixtures,
    )
    state.evidence_conflict = "conflict" in fixtures
    await write_audit(
        session,
        case_id=state.case_id,
        event="context.gathered",
        actor=AGENT,
        payload={
            "snapshot_hash": snapshot_hash(state.context.model_dump()),
            "context": state.context.model_dump(),
        },
    )
    return state


async def normalize(state: CaseState, deps: Deps, session: AsyncSession) -> CaseState:
    """Redact PII (S7) and flag injection (S8) before anything can reach an LLM."""
    state = state.model_copy(deep=True)
    _, inp = await _load_case(session, state)
    names: list[str] = []
    if state.context is not None:
        raw = await deps.fetch_context({"external_id": inp.external_id})
        names = [raw.rider_name] if raw.rider_name else []
    red = await deps.skills["redact_pii"]({"text": inp.complaint_text, "names": names})
    inj = await deps.skills["detect_injection"]({"text": inp.complaint_text})
    state.redacted_text, state.pii_fields_sent = red.redacted, red.fields_sent
    state.injection_detected, state.injection_matches = inj.detected, inj.matched
    state.mode_at_start, _ = await _effective_mode(session, state)
    await write_audit(
        session,
        case_id=state.case_id,
        event="input.normalized",
        actor=AGENT,
        payload={
            "input_hash": snapshot_hash(inp.model_dump(mode="json")),
            "redacted_text": red.redacted,
            "fields_sent": red.fields_sent,
            "injection_detected": inj.detected,
            "injection_matches": inj.matched,
        },
    )
    return state


async def _compute_calc(state: CaseState, deps: Deps) -> CalcSnapshot | None:
    ctx = state.context
    if ctx is None or state.case_type is None or ctx.payments is None:
        return None
    dup = 0
    if state.case_type is CaseType.DUPLICATE_CHARGE:
        pays = [
            {"id": str(i), "amount": a, "minutes_since_first": 2 * i}
            for i, a in enumerate(ctx.payments)
        ]
        dup = (await deps.skills["detect_duplicate_charge"]({"payments": pays})).duplicate_amount
    out = await deps.skills["calculate_refund"](
        {
            "case_type": state.case_type,
            "total": ctx.total,
            "facts": ctx.fixtures,
            "duplicate_amount": dup,
        }
    )
    return CalcSnapshot(
        outcome=out.outcome.value,
        amount=out.amount,
        disputed_charge=out.disputed_charge,
        reason_code=out.reason_code,
    )


async def triage(state: CaseState, deps: Deps, session: AsyncSession) -> CaseState:
    """Deterministic triage: clear-cut cases get a rule decision with no LLM call."""
    state = state.model_copy(deep=True)
    ctx, policy = state.context, deps.policies[state.region]
    state.calc = await _compute_calc(state, deps)
    if ctx is not None:
        fr = await deps.skills["score_fraud_signals"](
            {"prior_disputes": ctx.prior_disputes, "refunds_30d": ctx.refunds_30d}
        )
        state.fraud_score, state.fraud_signals = fr.score, fr.signals
    state.ambiguous = bool(HEDGES.search(state.redacted_text))
    is_fraud = state.fraud_score >= policy.thresholds.fraud_escalate_score
    if ctx is None:
        state.path, state.proposal = "rule_escalate", _rule_escalate("context unavailable")
    elif state.mode_at_start is AutonomyMode.KILL_SWITCH:
        state.path, state.proposal = "kill_switch", _rule_escalate("kill switch: LLM disabled")
    elif state.injection_detected:
        state.path, state.proposal = "rule_escalate", _rule_escalate("instruction-like text")
    elif ctx.payments is None:
        state.path = "rule"
        state.proposal = Proposal(
            outcome=Outcome.REQUEST_INFO,
            confidence=0.9,
            rationale="payment records missing",
            source="rule",
        )
    elif is_fraud or state.case_type is CaseType.SUSPICIOUS:
        state.path, state.proposal = "rule_escalate", _rule_escalate("fraud signals")
    elif state.ambiguous or state.case_type is None:
        state.path = "llm"
    else:
        calc = state.calc
        assert calc is not None  # payments present and case_type known
        state.path = "rule"
        state.proposal = Proposal(
            outcome=Outcome(calc.outcome),
            amount=calc.amount or None,
            confidence=0.95,
            rationale=f"rule: {calc.reason_code}",
            source="rule",
        )
    await write_audit(
        session,
        case_id=state.case_id,
        event="triage.done",
        actor=AGENT,
        payload={
            "path": state.path,
            "calc": state.calc.model_dump() if state.calc else None,
            "fraud_score": state.fraud_score,
            "fraud_signals": state.fraud_signals,
            "ambiguous": state.ambiguous,
        },
    )
    return state


def _meta(task: str, prompt: Prompt, res: Any) -> LLMMeta:
    if res.repaired:
        LLM_REPAIRS.inc()
    if res.fallback_used:
        LLM_FALLBACKS.inc()
    return LLMMeta(
        task=task,
        prompt_id=prompt.id,
        provider=res.provider,
        model=res.model,
        raw_text=res.raw_text,
        tokens_in=res.tokens_in,
        tokens_out=res.tokens_out,
        repaired=res.repaired,
        fallback_used=res.fallback_used,
    )


def _amount_for(basis: str, calc: CalcSnapshot | None) -> int:
    if calc is None:
        return 0
    return {
        "calculated": calc.amount,
        "disputed_full": calc.disputed_charge,
        "half_calculated": calc.amount // 2,
        "none": 0,
    }[basis]


async def llm_analysis(state: CaseState, deps: Deps, session: AsyncSession) -> CaseState:
    """Only for ambiguous/unclassified cases. Any LLM failure -> ESCALATE proposal (S10)."""
    if state.path != "llm":
        return state
    state = state.model_copy(deep=True)
    try:
        if state.case_type is None:
            cprompt = load_prompt("classify")
            cres = await deps.gateway.generate_structured(
                task="classify",
                prompt_id=cprompt.id,
                system=cprompt.text,
                user=wrap_untrusted(state.redacted_text),
                schema=ClassifyOutput,
            )
            state.llm.append(_meta("classify", cprompt, cres))
            state.case_type = cres.parsed.case_type
            if cres.parsed.instructions_in_text:
                state.injection_detected = True
            state.calc = await _compute_calc(state, deps)
        prompt = load_prompt("analyze")
        calc = state.calc
        rule = (
            f"{calc.reason_code}, amount {calc.amount}, disputed charge {calc.disputed_charge}"
            if calc
            else "not available"
        )
        user = (
            f"Case type: {state.case_type.value if state.case_type else 'unknown'}\n"
            f"Rule calculation: {rule}\n"
            f"Fraud signals: {', '.join(state.fraud_signals) or 'none'}\n"
            f"Complaint:\n{wrap_untrusted(state.redacted_text)}"
        )
        res = await deps.gateway.generate_structured(
            task="analyze",
            prompt_id=prompt.id,
            system=prompt.text,
            user=user,
            schema=AnalysisOutput,
        )
    except LLMError as exc:
        state.path = "llm_unavailable"
        state.proposal = _rule_escalate(f"llm unavailable: {type(exc).__name__}")
        await write_audit(
            session,
            case_id=state.case_id,
            event="llm.failed",
            actor=AGENT,
            payload={"error": type(exc).__name__},
        )
        return state
    out: AnalysisOutput = res.parsed
    state.llm.append(_meta("analyze", prompt, res))
    # Defense in depth: instruction-like text in the model's own output is also an injection signal.
    echoed = await deps.skills["detect_injection"]({"text": out.rationale})
    if out.instructions_in_text or echoed.detected:
        state.injection_detected = True
    if out.conflicting_evidence:
        state.evidence_conflict = True
    amount = _amount_for(out.amount_basis, state.calc)
    state.proposal = Proposal(
        outcome=out.outcome,
        amount=amount or None,
        confidence=out.confidence,
        rationale=out.rationale,
        source="llm",
    )
    await write_audit(
        session,
        case_id=state.case_id,
        event="llm.proposal",
        actor=AGENT,
        prompt_version=prompt.id,
        model=f"{res.provider}/{res.model}",
        payload={
            "raw_output": res.raw_text,
            "parsed": out.model_dump(mode="json"),
            "proposal": state.proposal.model_dump(mode="json"),
            "tokens": [res.tokens_in, res.tokens_out],
            "repaired": res.repaired,
            "fallback_used": res.fallback_used,
        },
    )
    return state


async def policy_step(state: CaseState, deps: Deps, session: AsyncSession) -> CaseState:
    state = state.model_copy(deep=True)
    assert state.proposal is not None, "triage/llm_analysis must set a proposal"
    policy = deps.policies[state.region]
    ctx = state.context
    mode, canary_pct = await _effective_mode(session, state)
    now = datetime.now(UTC)
    account = f"rider:{state.rider_ref}"
    paid = await net_paid(session, case_id=state.case_id)
    facts = CaseFacts(
        case_id=str(state.case_id),
        region=state.region,
        case_type=state.case_type or CaseType.SUSPICIOUS,
        refundable_remaining=max((ctx.total if ctx else 0) - paid, 0),
        day_spend=await day_spend(
            session,
            region=state.region,
            since=now.replace(hour=0, minute=0, second=0, microsecond=0),
        ),
        customer_refunds_30d=max(
            ctx.refunds_30d if ctx else 0,
            await payout_count(session, account=account, since=now - timedelta(days=30)),
        ),
        fraud_score=state.fraud_score,
        evidence_conflict=state.evidence_conflict,
        injection_detected=state.injection_detected,
        mode=mode,
        canary_hit=canary_hit(str(state.case_id), canary_pct),
    )
    state.facts, state.policy_version = facts, policy.version
    state.decision = evaluate(policy, state.proposal, facts)
    for rule in state.decision.violations:
        POLICY_BLOCKED.labels(rule.split("_")[0]).inc()
    await write_audit(
        session,
        case_id=state.case_id,
        event="policy.decision",
        actor="policy-engine",
        payload={
            "policy_version": policy.version,
            "facts": facts.model_dump(mode="json"),
            "proposal": state.proposal.model_dump(mode="json"),
            "decision": state.decision.model_dump(mode="json"),
        },
    )
    return state


async def execute(state: CaseState, deps: Deps, session: AsyncSession) -> CaseState:
    """Act on the policy decision. Atomic with the state snapshot; ledger is idempotent."""
    state = state.model_copy(deep=True)
    assert state.decision is not None and state.proposal is not None
    d, policy = state.decision, deps.policies[state.region]
    row = Decision(
        case_id=state.case_id,
        source=state.proposal.source,
        outcome=d.outcome.value,
        amount=d.amount,
        confidence=state.proposal.confidence,
        rationale=state.proposal.rationale,
        policy_rules_fired=d.rules_fired,
        route=d.route.value,
    )
    session.add(row)
    await session.flush()
    state.decision_id = row.id

    if not d.execute:
        state.final_status = "shadow_logged"
        await write_audit(
            session,
            case_id=state.case_id,
            event="action.skipped_shadow",
            actor=AGENT,
            payload={"would_route": d.route.value},
        )
    elif d.route is Route.AUTO_EXECUTE:
        if d.outcome in MONEY_OUTCOMES:
            tool = "issue_credit" if d.outcome is Outcome.CREDIT else "issue_refund"
            args = {
                "case_id": str(state.case_id),
                "version": 1,
                "account": f"rider:{state.rider_ref}",
                "amount": d.amount,
                "currency": policy.currency,
            }
            try:
                res = await deps.tools.call(
                    tool, agent_principal(policy), args, ToolContext(session, state.region)
                )
            except OpsPodError as exc:  # tool layer refused: fail safe to a human
                POLICY_BLOCKED.labels("X001").inc()
                state.decision = d.model_copy(
                    update={
                        "route": Route.ESCALATE,
                        "outcome": Outcome.ESCALATE,
                        "amount": 0,
                        "rules_fired": [*d.rules_fired, "X001_tool_refused"],
                    }
                )
                row.route, row.outcome, row.amount = Route.ESCALATE.value, "ESCALATE", 0
                state.final_status = "escalated"
                await write_audit(
                    session,
                    case_id=state.case_id,
                    event="action.refused",
                    actor=AGENT,
                    payload={"tool": tool, "error": str(exc)[:200]},
                )
            else:
                state.action_id = uuid.UUID(res["action_id"])
                state.final_status = "resolved"
                await write_audit(
                    session,
                    case_id=state.case_id,
                    event="action.executed",
                    actor=AGENT,
                    payload={"tool": tool, "args": args, **res},
                )
        else:
            state.final_status = "resolved"
            await write_audit(
                session,
                case_id=state.case_id,
                event="action.non_money",
                actor=AGENT,
                payload={"outcome": d.outcome.value},
            )
    elif d.route is Route.NEEDS_APPROVAL:
        session.add(Approval(case_id=state.case_id, decision_id=row.id, status="pending"))
        state.final_status = "awaiting_approval"
        await write_audit(
            session,
            case_id=state.case_id,
            event="approval.requested",
            actor=AGENT,
            payload={"decision_id": str(row.id), "amount": d.amount},
        )
    else:
        queue = "fraud" if FRAUD_RULES & set(d.rules_fired) else "general"
        state.final_status = "escalated"
        await write_audit(
            session,
            case_id=state.case_id,
            event="case.escalated",
            actor=AGENT,
            payload={"queue": queue, "rules": d.rules_fired, "reason": state.proposal.rationale},
        )
    return state


async def notify(state: CaseState, deps: Deps, session: AsyncSession) -> CaseState:
    """Templated customer message (simulated send). LLM may add one tone sentence only."""
    state = state.model_copy(deep=True)
    d = state.decision
    assert d is not None
    if state.final_status not in {"resolved", "escalated"}:
        return state  # shadow or pending approval: nothing to tell the customer yet
    _, inp = await _load_case(session, state)
    name = ""
    if state.context is not None:
        name = (await deps.fetch_context({"external_id": inp.external_id})).rider_name
    tone: str | None = None
    if deps.llm_tone and not state.injection_detected:
        try:
            prompt = load_prompt("draft_message")
            res = await deps.gateway.generate_structured(
                task="draft_message",
                prompt_id=prompt.id,
                system=prompt.text,
                user=f"Outcome type: {d.outcome.value}",
                schema=ToneOutput,
            )
            tone = res.parsed.sentence
            state.llm.append(_meta("draft_message", prompt, res))
        except LLMError:
            tone = None  # wording is optional; never fail the case over it
    out: MessageOutput = await deps.skills["draft_customer_message"](
        {
            "outcome": d.outcome,
            "amount": d.amount,
            "currency": deps.policies[state.region].currency,
            "rider_name": "[RIDER_1]",
            "reference": state.external_id,
            "sla_hours": deps.policies[state.region].sla_hours,
            "tone_sentence": tone,
        }
    )
    sent = rehydrate(out.body, {"[RIDER_1]": name} if name else {})
    state.message = out.body  # tokenized; the real name exists only in the sent copy
    outbox = deps.extras.get("outbox")
    if isinstance(outbox, list):
        outbox.append({"case_id": str(state.case_id), "body": sent})
    await write_audit(
        session,
        case_id=state.case_id,
        event="customer.message",
        actor=AGENT,
        payload={
            "body_tokenized": out.body,
            "sent_sha256": hashlib.sha256(sent.encode()).hexdigest(),
            "tone_used": out.tone_used,
        },
    )
    return state


async def finalize(state: CaseState, deps: Deps, session: AsyncSession) -> CaseState:
    state = state.model_copy(deep=True)
    case, _ = await _load_case(session, state)
    case.status = state.final_status or "escalated"
    await write_audit(
        session,
        case_id=state.case_id,
        event="pipeline.finalized",
        actor=AGENT,
        payload={
            "status": case.status,
            "path": state.path,
            "route": state.decision.route.value if state.decision else None,
        },
    )
    return state
