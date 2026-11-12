"""The release gate must pass a healthy run and block a deliberately bad prompt change."""

import pytest

from opspod.evals import harness, metrics

pytestmark = pytest.mark.integration


def subset():
    cases = harness.load_cases("dev")
    pick = {"ambiguous": 5, "injection": 3, "fraud": 3, "clear_cut": 8, "boundary": 6}
    out, seen = [], dict.fromkeys(pick, 0)
    for c in cases:
        for tag, cap in pick.items():
            if tag in c["tags"] and seen[tag] < cap:
                seen[tag] += 1
                out.append(c)
                break
    return out


async def test_gate_passes_good_and_blocks_regressed_prompts():
    cases = subset()
    good_results, good_events = await harness.run_eval(cases, llm="stub", stub="good")
    good = metrics.compute(good_results, good_events)
    assert good["policy_compliance_safety"] == 1.0 and good["correctness"] == 1.0
    assert metrics.gate(good, None) == []
    baseline = good

    refund_results, refund_events = await harness.run_eval(cases, llm="stub", stub="bad_refund")
    fails = metrics.gate(metrics.compute(refund_results, refund_events), baseline)
    assert any("correctness dropped" in f for f in fails)

    bad_results, bad_events = await harness.run_eval(cases, llm="stub", stub="malformed")
    bad = metrics.compute(bad_results, bad_events)
    assert bad["schema_valid_rate"] == 0.0
    assert any("schema-valid" in f for f in metrics.gate(bad, baseline))
    # safety never degrades: a broken model fails safe to humans rather than paying
    assert bad["policy_compliance_safety"] == 1.0
    assert bad["injection_unauthorized_actions"] == 0


async def test_every_injection_case_never_pays_with_even_a_hostile_model():
    cases = [c for c in harness.load_cases("all") if "injection" in c["tags"]]
    assert len(cases) >= 10
    results, events = await harness.run_eval(cases, llm="stub", stub="bad_refund")
    m = metrics.compute(results, events)
    assert m["injection_unauthorized_actions"] == 0 and m["adversarial_escalation_recall"] == 1.0
