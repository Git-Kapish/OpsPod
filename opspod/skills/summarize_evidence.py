"""summarize_evidence: structured evidence digest for approvers/escalations.

Deterministic bullets now; the LLM may later rewrite the prose from the redacted bullets only
(the gateway is wired in Phase 4). The bullets are the source of truth either way.
"""

from pydantic import BaseModel

from opspod.skills.base import Skill


class EvidenceInput(BaseModel):
    case_type: str
    redacted_complaint: str
    calc_reason: str
    calc_amount: int
    currency: str
    fraud_signals: list[str] = []
    injection_detected: bool = False
    conflict: str | None = None


class EvidenceOutput(BaseModel):
    bullets: list[str]
    summary: str


def _summarize(inp: EvidenceInput) -> EvidenceOutput:
    bullets = [
        f"Case type: {inp.case_type}",
        f"Rule calculation: {inp.calc_reason}, {inp.calc_amount} minor units {inp.currency}",
        f"Complaint (redacted): {inp.redacted_complaint[:300]}",
    ]
    if inp.fraud_signals:
        bullets.append("Fraud signals: " + ", ".join(inp.fraud_signals))
    if inp.injection_detected:
        bullets.append("WARNING: complaint contains instruction-like text (possible injection)")
    if inp.conflict:
        bullets.append(f"Conflicting evidence: {inp.conflict}")
    return EvidenceOutput(bullets=bullets, summary="\n".join(f"- {b}" for b in bullets))


SKILL = Skill("summarize_evidence", "1.0.0", EvidenceInput, EvidenceOutput, _summarize)
