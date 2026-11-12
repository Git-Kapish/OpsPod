from opspod.skills import (
    calculate_refund,
    detect_duplicate_charge,
    detect_injection,
    draft_customer_message,
    redact_pii,
    score_fraud_signals,
    summarize_evidence,
)
from opspod.skills.base import Skill

# fetch_trip_context needs a repo, so it is built with fetch_trip_context.make_skill(repo).
STATIC_SKILLS: dict[str, Skill] = {
    m.SKILL.name: m.SKILL
    for m in (
        calculate_refund,
        detect_duplicate_charge,
        detect_injection,
        draft_customer_message,
        redact_pii,
        score_fraud_signals,
        summarize_evidence,
    )
}
