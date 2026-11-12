"""Typed pipeline state. Snapshotted to case_steps after every step; contains no raw PII."""

import uuid
from typing import Any

from pydantic import BaseModel, Field

from opspod.domain.enums import AutonomyMode, CaseType
from opspod.policy.models import CaseFacts, PolicyDecision, Proposal


class CaseIn(BaseModel):
    """Intake payload (POST /v1/cases). Stored raw in cases.payload_json."""

    external_id: str  # id in the source system; keys context lookup
    case_type: CaseType | None = None  # None -> LLM classify
    region: str
    rider_ref: str
    complaint_text: str = Field(max_length=4000)
    claimed_amount: int | None = Field(default=None, ge=0)


class ContextSnapshot(BaseModel):
    currency: str
    total: int
    payments: list[int] | None
    prior_disputes: int
    refunds_30d: int
    fixtures: dict[str, Any]  # rider_name removed before persisting


class CalcSnapshot(BaseModel):
    outcome: str
    amount: int
    disputed_charge: int
    reason_code: str


class LLMMeta(BaseModel):
    task: str
    prompt_id: str
    provider: str
    model: str
    raw_text: str
    tokens_in: int
    tokens_out: int
    repaired: bool
    fallback_used: bool


class CaseState(BaseModel):
    case_id: uuid.UUID
    external_id: str
    region: str
    rider_ref: str
    case_type: CaseType | None
    completed: list[str] = []
    # normalize
    redacted_text: str = ""
    pii_fields_sent: dict[str, int] = {}
    injection_detected: bool = False
    injection_matches: list[str] = []
    mode_at_start: AutonomyMode = AutonomyMode.ASSISTED
    # gather_context / triage
    context: ContextSnapshot | None = None
    calc: CalcSnapshot | None = None
    fraud_score: int = 0
    fraud_signals: list[str] = []
    ambiguous: bool = False
    evidence_conflict: bool = False
    path: str = ""  # rule | llm | rule_escalate | llm_unavailable | kill_switch
    # llm_analysis / policy
    proposal: Proposal | None = None
    llm: list[LLMMeta] = []
    facts: CaseFacts | None = None
    policy_version: int | None = None
    decision: PolicyDecision | None = None
    # execute / notify
    decision_id: uuid.UUID | None = None
    action_id: uuid.UUID | None = None
    final_status: str = ""
    message: str | None = None
