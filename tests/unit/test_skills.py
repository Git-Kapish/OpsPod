import json
from pathlib import Path

import pytest

from opspod.domain.enums import CaseType, Outcome
from opspod.skills import STATIC_SKILLS
from opspod.skills.detect_duplicate_charge import SKILL as DUP
from opspod.skills.detect_injection import SKILL as INJ
from opspod.skills.draft_customer_message import SKILL as MSG
from opspod.skills.fetch_trip_context import ContextNotFound, FixtureRepo, make_skill
from opspod.skills.redact_pii import SKILL as REDACT
from opspod.skills.redact_pii import rehydrate
from opspod.skills.score_fraud_signals import SKILL as FRAUD

CALC = STATIC_SKILLS["calculate_refund"]
DATA = Path(__file__).parents[2] / "evals" / "datasets"
CASES = [json.loads(line) for f in DATA.glob("*.jsonl") for line in f.read_text().splitlines()]


def test_all_skills_versioned():
    assert len(STATIC_SKILLS) == 7
    assert all(s.version.count(".") == 2 for s in STATIC_SKILLS.values())


async def test_redact_removes_all_pii_and_rehydrates():
    text = (
        "Hi, this is Asha Rao (asha.rao@example.com), phone +1 415-555-0199, "
        "card ending 4242, I live at 42 Park Avenue. Asha was charged twice."
    )
    out = await REDACT({"text": text, "names": ["Asha Rao"]})
    for pii in ("Asha", "Rao", "asha.rao@example.com", "415-555-0199", "4242", "42 Park Avenue"):
        assert pii not in out.redacted
    assert "[RIDER_1]" in out.redacted and "[EMAIL_1]" in out.redacted
    assert rehydrate(out.redacted, out.mapping).count("Asha") >= 1
    assert "asha.rao@example.com" not in json.dumps(out.fields_sent)


@pytest.mark.parametrize("case", [c for c in CASES if "injection" in c["tags"]])
async def test_every_dataset_injection_detected(case):
    assert (await INJ({"text": case["input"]["complaint_text"]})).detected


@pytest.mark.parametrize("case", [c for c in CASES if "injection" not in c["tags"]])
async def test_no_false_positive_on_normal_complaints(case):
    assert not (await INJ({"text": case["input"]["complaint_text"]})).detected


async def test_duplicate_detection_window():
    pays = [
        {"id": "a", "amount": 500, "minutes_since_first": 0},
        {"id": "b", "amount": 500, "minutes_since_first": 2},
        {"id": "c", "amount": 700, "minutes_since_first": 3},
        {"id": "d", "amount": 500, "minutes_since_first": 600},
    ]
    out = await DUP({"payments": pays})
    assert (
        out.duplicate_found and out.duplicate_payment_ids == ["b"] and out.duplicate_amount == 500
    )
    assert not (await DUP({"payments": pays[:1]})).duplicate_found


async def test_fraud_score_monotonic_and_capped():
    low = await FRAUD({"prior_disputes": 0, "refunds_30d": 0})
    high = await FRAUD({"prior_disputes": 20, "refunds_30d": 9, "claimed_exceeds_total": True})
    assert low.score == 0 and high.score == 100


async def test_message_blocks_commitments_in_tone():
    base = {"outcome": Outcome.DENY, "reference": "R1"}
    for bad in ("We will refund you double!", "Sorry about the 2 hour wait", "x" * 200):
        assert not (await MSG({**base, "tone_sentence": bad})).tone_used
    ok = await MSG({**base, "tone_sentence": "Thanks for your patience."})
    assert ok.tone_used and "Thanks for your patience." in ok.body


async def test_message_formats_amount():
    out = await MSG({"outcome": Outcome.FULL_REFUND, "amount": 1205, "reference": "R"})
    assert "12.05 USD" in out.body


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if c["tags"][0] == "clear_cut" and c["case_type"] != "suspicious"],
)
async def test_calculate_refund_matches_golden_labels(case):
    fx = case["fixtures"]
    dup = 0
    if case["case_type"] == "duplicate_charge":
        pays = [
            {"id": str(i), "amount": a, "minutes_since_first": i}
            for i, a in enumerate(fx["payments"])
        ]
        dup = (await DUP({"payments": pays})).duplicate_amount
    out = await CALC(
        {
            "case_type": CaseType(case["case_type"]),
            "total": fx["total"],
            "facts": fx,
            "duplicate_amount": dup,
        }
    )
    exp = case["expected"]
    assert (out.outcome.value, out.amount) == (exp["outcome"], exp["amount"])


async def test_calculate_refund_never_exceeds_disputed_charge():
    out = await CALC(
        {
            "case_type": "missing_item",
            "total": 100,
            "facts": {"items": [{"name": "x", "price": 999}], "missing": "x"},
        }
    )
    assert out.amount == 100 and out.outcome is Outcome.FULL_REFUND


async def test_fixture_repo_context():
    repo = FixtureRepo.from_datasets()
    skill = make_skill(repo)
    ctx = await skill({"external_id": CASES[0]["input"]["external_id"]})
    assert ctx.rider_name and ctx.currency
    with pytest.raises(ContextNotFound):
        await skill({"external_id": "nope"})
