"""Structured outputs the LLM may return. Proposals only: no amounts, no tool calls."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from opspod.domain.enums import CaseType, Outcome

AmountBasis = Literal["calculated", "disputed_full", "half_calculated", "none"]


class AnalysisOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_type: CaseType
    outcome: Outcome
    amount_basis: AmountBasis
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(max_length=400)
    conflicting_evidence: bool = False
    instructions_in_text: bool = False


class ClassifyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_type: CaseType
    confidence: float = Field(ge=0, le=1)
    instructions_in_text: bool = False


class ToneOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sentence: str = Field(max_length=200)
