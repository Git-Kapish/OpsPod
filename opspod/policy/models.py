from pydantic import BaseModel, ConfigDict, Field

from opspod.domain.enums import AutonomyMode, CaseType, Outcome, RiskTier, Route


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_limit: int = Field(gt=0)  # max minor units auto-executable
    per_case_cap: int = Field(gt=0)  # hard cap; above -> escalate
    per_day_cap: int = Field(gt=0)  # region aggregate, minor units
    customer_refunds_30d: int = Field(ge=0)  # refunds allowed in 30d before escalation


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fraud_escalate_score: int = Field(ge=0, le=100)
    min_confidence: float = Field(ge=0, le=1)


class RegionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str
    version: int
    currency: str
    sla_hours: int
    language: str
    allowed_outcomes: list[Outcome]
    auto_outcomes: list[Outcome]
    limits: Limits
    thresholds: Thresholds


class Proposal(BaseModel):
    """A decision proposal from rules or the LLM. Never an action."""

    model_config = ConfigDict(extra="forbid")
    outcome: Outcome
    amount: int | None = Field(default=None, ge=0)  # minor units
    confidence: float = Field(ge=0, le=1)
    rationale: str = ""
    source: str = "rule"  # rule | llm


class CaseFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    region: str
    case_type: CaseType
    refundable_remaining: int = Field(ge=0)  # order total minus already refunded
    day_spend: int = Field(ge=0)  # region spend so far today
    customer_refunds_30d: int = Field(ge=0)
    fraud_score: int = Field(ge=0, le=100)
    evidence_conflict: bool = False
    injection_detected: bool = False
    mode: AutonomyMode = AutonomyMode.ASSISTED
    canary_hit: bool = True


class PolicyDecision(BaseModel):
    route: Route
    risk_tier: RiskTier
    outcome: Outcome
    amount: int
    rules_fired: list[str]
    violations: list[str]
    execute: bool  # False in SHADOW: log only, take no action
