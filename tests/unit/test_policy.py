import pytest
from hypothesis import given
from hypothesis import strategies as st

from opspod.core.errors import ConfigError
from opspod.domain.enums import AutonomyMode, CaseType, Outcome, RiskTier, Route
from opspod.policy.canary import canary_hit
from opspod.policy.engine import evaluate
from opspod.policy.loader import load_all, load_policy
from opspod.policy.models import CaseFacts, Proposal

US = load_policy("us")


def facts(**kw) -> CaseFacts:
    base = dict(
        case_id="c1",
        region="us",
        case_type=CaseType.DUPLICATE_CHARGE,
        refundable_remaining=5000,
        day_spend=0,
        customer_refunds_30d=0,
        fraud_score=0,
        mode=AutonomyMode.AUTO_LOW_RISK,
    )
    base.update(kw)
    return CaseFacts(**base)


def prop(outcome=Outcome.FULL_REFUND, amount=1000, conf=0.95) -> Proposal:
    return Proposal(outcome=outcome, amount=amount, confidence=conf)


def test_low_risk_auto():
    d = evaluate(US, prop(), facts())
    assert (d.route, d.risk_tier, d.execute) == (Route.AUTO_EXECUTE, RiskTier.LOW, True)
    assert d.amount == 1000


def test_above_auto_limit_needs_approval():
    d = evaluate(US, prop(amount=5000), facts())
    assert d.route is Route.NEEDS_APPROVAL and d.risk_tier is RiskTier.MEDIUM


def test_assisted_mode_never_auto():
    d = evaluate(US, prop(), facts(mode=AutonomyMode.ASSISTED))
    assert d.route is Route.NEEDS_APPROVAL
    assert "A002_mode_requires_approval" in d.rules_fired


def test_canary_miss_falls_back_to_approval():
    d = evaluate(US, prop(), facts(canary_hit=False))
    assert d.route is Route.NEEDS_APPROVAL


def test_shadow_computes_auto_but_does_not_execute():
    d = evaluate(US, prop(), facts(mode=AutonomyMode.SHADOW, canary_hit=False))
    assert d.route is Route.AUTO_EXECUTE and d.execute is False


def test_kill_switch_escalates_everything():
    d = evaluate(US, prop(), facts(mode=AutonomyMode.KILL_SWITCH))
    assert d.route is Route.ESCALATE and "M001_kill_switch" in d.rules_fired


@pytest.mark.parametrize(
    ("proposal", "fkw", "rule"),
    [
        (prop(Outcome.ESCALATE, None), {}, "R001_proposal_escalates"),
        (prop(amount=None), {}, "P011_money_outcome_needs_positive_amount"),
        (prop(Outcome.DENY, 100), {}, "P011_amount_on_non_money_outcome"),
        (prop(amount=6000), {"refundable_remaining": 5000}, "P002_exceeds_refundable_remaining"),
        (prop(amount=10001), {"refundable_remaining": 99999}, "P003_exceeds_per_case_cap"),
        (prop(), {"day_spend": 5000000}, "P004_exceeds_day_cap"),
        (prop(), {"customer_refunds_30d": 3}, "P005_customer_refund_frequency"),
        (prop(), {"fraud_score": 60}, "P006_fraud_score"),
        (prop(conf=0.69), {}, "P007_low_confidence"),
        (prop(), {"evidence_conflict": True}, "P008_conflicting_evidence"),
        (prop(), {"injection_detected": True}, "P009_injection_detected"),
    ],
)
def test_each_rule_escalates(proposal, fkw, rule):
    d = evaluate(US, proposal, facts(**fkw))
    assert d.route is Route.ESCALATE and d.amount == 0 and d.outcome is Outcome.ESCALATE
    assert rule in d.rules_fired


def test_outcome_not_allowed_is_violation():
    pol = US.model_copy(update={"allowed_outcomes": [Outcome.ESCALATE]})
    d = evaluate(pol, prop(), facts())
    assert d.route is Route.ESCALATE and "P001_outcome_not_allowed" in d.violations


def test_regions_differ():
    pols = load_all()
    assert {"us", "eu", "in"} <= set(pols)
    # EU: DENY needs a human; US allows auto DENY.
    deny = prop(Outcome.DENY, None)
    assert evaluate(pols["us"], deny, facts()).route is Route.AUTO_EXECUTE
    eu = evaluate(pols["eu"], deny, facts(region="eu"))
    assert eu.route is Route.NEEDS_APPROVAL


def test_unknown_region():
    with pytest.raises(ConfigError):
        load_policy("zz")


@given(
    amount=st.integers(0, 50_000),
    remaining=st.integers(0, 50_000),
    fraud=st.integers(0, 100),
    conf=st.floats(0, 1),
    mode=st.sampled_from(list(AutonomyMode)),
)
def test_never_pays_beyond_limits(amount, remaining, fraud, conf, mode):
    d = evaluate(
        US,
        Proposal(outcome=Outcome.PARTIAL_REFUND, amount=amount, confidence=conf),
        facts(refundable_remaining=remaining, fraud_score=fraud, mode=mode),
    )
    if d.route is Route.AUTO_EXECUTE:
        assert d.amount <= min(US.limits.auto_limit, remaining)
        assert mode in (AutonomyMode.AUTO_LOW_RISK, AutonomyMode.SHADOW)
        assert fraud < US.thresholds.fraud_escalate_score
    if d.route is not Route.ESCALATE:
        assert d.amount <= min(US.limits.per_case_cap, remaining)


def test_canary_is_stable_and_proportional():
    assert canary_hit("x", 100) and not canary_hit("x", 0)
    assert canary_hit("abc", 30) == canary_hit("abc", 30)
    hits = sum(canary_hit(f"case-{i}", 25) for i in range(2000))
    assert 400 < hits < 600
