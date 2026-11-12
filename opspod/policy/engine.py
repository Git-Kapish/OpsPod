"""Deterministic policy engine: the only place routes are decided.

The LLM (or rules) proposes; this validates the proposal against regional policy,
computes a risk tier and picks a route. It never raises the amount and never trusts
the proposal: any violation falls back to ESCALATE (fail-safe, a human decides).
Pure function: usage counters (day spend, customer history) are passed in via CaseFacts.
"""

from opspod.domain.enums import MONEY_OUTCOMES, AutonomyMode, Outcome, RiskTier, Route
from opspod.policy.models import CaseFacts, PolicyDecision, Proposal, RegionPolicy


def evaluate(policy: RegionPolicy, proposal: Proposal, facts: CaseFacts) -> PolicyDecision:
    violations: list[str] = []
    escalate: list[str] = []

    if facts.mode is AutonomyMode.KILL_SWITCH:
        escalate.append("M001_kill_switch")
    if proposal.outcome is Outcome.ESCALATE:
        escalate.append("R001_proposal_escalates")
    if proposal.outcome not in policy.allowed_outcomes:
        violations.append("P001_outcome_not_allowed")

    amount = proposal.amount or 0
    is_money = proposal.outcome in MONEY_OUTCOMES
    if is_money and amount <= 0:
        violations.append("P011_money_outcome_needs_positive_amount")
    if not is_money and amount > 0:
        violations.append("P011_amount_on_non_money_outcome")
    if is_money:
        lim = policy.limits
        if amount > facts.refundable_remaining:
            violations.append("P002_exceeds_refundable_remaining")
        if amount > lim.per_case_cap:
            violations.append("P003_exceeds_per_case_cap")
        if facts.day_spend + amount > lim.per_day_cap:
            violations.append("P004_exceeds_day_cap")
        if facts.customer_refunds_30d >= lim.customer_refunds_30d:
            escalate.append("P005_customer_refund_frequency")

    th = policy.thresholds
    if facts.fraud_score >= th.fraud_escalate_score:
        escalate.append("P006_fraud_score")
    if proposal.confidence < th.min_confidence:
        escalate.append("P007_low_confidence")
    if facts.evidence_conflict:
        escalate.append("P008_conflicting_evidence")
    if facts.injection_detected:
        escalate.append("P009_injection_detected")

    fired = violations + escalate
    if fired:
        return PolicyDecision(
            route=Route.ESCALATE,
            risk_tier=RiskTier.HIGH,
            outcome=Outcome.ESCALATE,
            amount=0,
            rules_fired=fired,
            violations=violations,
            execute=facts.mode is not AutonomyMode.SHADOW,
        )

    low = proposal.outcome in policy.auto_outcomes and amount <= policy.limits.auto_limit
    llm_blocked = low and proposal.source == "llm" and not policy.llm_proposals_auto
    if llm_blocked:
        low = False
    tier = RiskTier.LOW if low else RiskTier.MEDIUM
    shadow = facts.mode is AutonomyMode.SHADOW
    # Shadow computes what AUTO_LOW_RISK would do (to measure agreement) but acts on nothing.
    auto_mode = shadow or (facts.mode is AutonomyMode.AUTO_LOW_RISK and facts.canary_hit)
    if low and auto_mode:
        route, fired = Route.AUTO_EXECUTE, ["A001_low_risk_auto"]
    elif llm_blocked:
        route, fired = Route.NEEDS_APPROVAL, ["A004_llm_proposal_needs_review"]
    elif not low:
        route, fired = Route.NEEDS_APPROVAL, ["A003_above_auto_limit_or_outcome"]
    else:
        route, fired = Route.NEEDS_APPROVAL, ["A002_mode_requires_approval"]
    return PolicyDecision(
        route=route,
        risk_tier=tier,
        outcome=proposal.outcome,
        amount=amount,
        rules_fired=fired,
        violations=[],
        execute=not shadow,
    )
