"""calculate_refund: exact refund math per case type. Integer minor units only; no LLM."""

from typing import Any

from pydantic import BaseModel, Field

from opspod.domain.enums import CaseType, Outcome
from opspod.skills.base import Skill


class RefundInput(BaseModel):
    case_type: CaseType
    total: int = Field(ge=0)  # order/trip total charged, minor units
    facts: dict[str, Any] = {}
    duplicate_amount: int = Field(default=0, ge=0)  # from detect_duplicate_charge


class RefundOutput(BaseModel):
    outcome: Outcome
    amount: int
    disputed_charge: int
    reason_code: str


def _result(amount: int, disputed: int, code: str) -> RefundOutput:
    if amount <= 0:
        return RefundOutput(
            outcome=Outcome.DENY, amount=0, disputed_charge=disputed, reason_code=code
        )
    amount = min(amount, disputed)  # never refund more than the disputed charge
    outcome = Outcome.FULL_REFUND if amount == disputed else Outcome.PARTIAL_REFUND
    return RefundOutput(outcome=outcome, amount=amount, disputed_charge=disputed, reason_code=code)


def _calc(inp: RefundInput) -> RefundOutput:
    f, total = inp.facts, inp.total
    match inp.case_type:
        case CaseType.WRONG_FARE:
            return _result(total - f.get("expected_fare", total), total, "fare_over_quote")
        case CaseType.DUPLICATE_CHARGE:
            return _result(inp.duplicate_amount, total, "duplicate_payment")
        case CaseType.MISSING_ITEM:
            missing = f.get("missing")
            price = sum(i["price"] for i in f.get("items", []) if i["name"] == missing)
            return _result(price, total, "item_missing")
        case CaseType.LATE_CANCEL_FEE:
            fee = f.get("fee", 0)
            if f.get("driver_arrived_before_cancel"):
                return _result(0, fee, "fee_valid_driver_arrived")
            return _result(fee, fee, "fee_waived_prompt_cancel")
        case CaseType.NO_SHOW:
            if f.get("driver_gps_at_pickup", True):
                return _result(0, total, "driver_was_at_pickup")
            return _result(total, total, "driver_no_show")
        case CaseType.PROMO_NOT_APPLIED:
            promo = f.get("promo") or {}
            return _result(promo.get("discount", 0) if promo.get("eligible") else 0, total, "promo")
        case _:
            return _result(0, total, "not_calculable")


SKILL = Skill("calculate_refund", "1.0.0", RefundInput, RefundOutput, _calc)
