"""draft_customer_message: templated customer reply. An LLM may only supply one tone sentence.

The tone sentence is dropped unless it is short, digit-free and makes no commitments, so the
model can never promise money or change the outcome (S8). Amounts come from the decision.
"""

import re

from pydantic import BaseModel

from opspod.domain.enums import Outcome
from opspod.skills.base import Skill

TEMPLATES = {
    Outcome.FULL_REFUND: "We have refunded {amount} to your original payment method.",
    Outcome.PARTIAL_REFUND: "We have refunded {amount} to your original payment method.",
    Outcome.CREDIT: "We have added {amount} in credit to your account.",
    Outcome.DENY: "After review, we cannot refund this charge. Reply if you have more details.",
    Outcome.REQUEST_INFO: "Could you share more details so we can look into this?",
    Outcome.ESCALATE: "Your case is with a specialist who will follow up within {sla_hours} hours.",
}
COMMITMENT = re.compile(r"(?i)\b(refund|credit|guarantee|promise|will pay|compensat|reimburs)")


class MessageInput(BaseModel):
    outcome: Outcome
    amount: int = 0  # minor units
    currency: str = "USD"
    rider_name: str = ""
    reference: str
    sla_hours: int = 24
    tone_sentence: str | None = None


class MessageOutput(BaseModel):
    body: str
    tone_used: bool


def _tone_ok(s: str | None) -> bool:
    if not s:
        return False
    return len(s) <= 160 and not re.search(r"\d", s) and not COMMITMENT.search(s)


def format_amount(minor: int, currency: str) -> str:
    return f"{minor // 100}.{minor % 100:02d} {currency}"


def _draft(inp: MessageInput) -> MessageOutput:
    greeting = f"Hi {inp.rider_name}," if inp.rider_name else "Hi,"
    core = TEMPLATES[inp.outcome].format(
        amount=format_amount(inp.amount, inp.currency), sla_hours=inp.sla_hours
    )
    tone = inp.tone_sentence if _tone_ok(inp.tone_sentence) else None
    parts = [greeting, tone, core, f"Reference: {inp.reference}"]
    return MessageOutput(body="\n".join(p for p in parts if p), tone_used=tone is not None)


SKILL = Skill("draft_customer_message", "1.0.0", MessageInput, MessageOutput, _draft)
