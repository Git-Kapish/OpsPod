"""score_fraud_signals: deterministic risk score 0-100. Signals decide; an LLM may only explain."""

from pydantic import BaseModel, Field

from opspod.skills.base import Skill


class FraudInput(BaseModel):
    prior_disputes: int = Field(ge=0)
    refunds_30d: int = Field(ge=0)
    claimed_exceeds_total: bool = False
    account_age_days: int | None = None


class FraudOutput(BaseModel):
    score: int = Field(ge=0, le=100)
    signals: list[str]


def _score(inp: FraudInput) -> FraudOutput:
    score, signals = 0, []
    if inp.prior_disputes:
        score += 15 * inp.prior_disputes
        signals.append(f"prior_disputes={inp.prior_disputes}")
    if inp.refunds_30d > 1:
        score += 10 * (inp.refunds_30d - 1)
        signals.append(f"refunds_30d={inp.refunds_30d}")
    if inp.claimed_exceeds_total:
        score += 25
        signals.append("claim_exceeds_total")
    if inp.account_age_days is not None and inp.account_age_days < 7:
        score += 20
        signals.append("new_account")
    return FraudOutput(score=min(score, 100), signals=signals)


SKILL = Skill("score_fraud_signals", "1.0.0", FraudInput, FraudOutput, _score)
