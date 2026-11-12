"""detect_duplicate_charge: exact-match duplicate payments within a time window."""

from pydantic import BaseModel, Field

from opspod.skills.base import Skill


class PaymentRecord(BaseModel):
    id: str
    amount: int = Field(ge=0)  # minor units
    minutes_since_first: int = Field(ge=0)


class DupInput(BaseModel):
    payments: list[PaymentRecord]
    window_minutes: int = 30


class DupOutput(BaseModel):
    duplicate_found: bool
    duplicate_amount: int
    duplicate_payment_ids: list[str]  # the later copies, i.e. the ones to refund


def _detect(inp: DupInput) -> DupOutput:
    seen: dict[int, PaymentRecord] = {}
    dups: list[PaymentRecord] = []
    for p in sorted(inp.payments, key=lambda p: (p.minutes_since_first, p.id)):
        first = seen.get(p.amount)
        if first is None:
            seen[p.amount] = p
        elif p.minutes_since_first - first.minutes_since_first <= inp.window_minutes:
            dups.append(p)
    return DupOutput(
        duplicate_found=bool(dups),
        duplicate_amount=sum(p.amount for p in dups),
        duplicate_payment_ids=[p.id for p in dups],
    )


SKILL = Skill("detect_duplicate_charge", "1.0.0", DupInput, DupOutput, _detect)
