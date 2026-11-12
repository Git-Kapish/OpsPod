"""redact_pii: replace names, emails, phones, addresses and card fragments with tokens.

Runs before any LLM call (S7). Mapping stays server-side; rehydrate() is for templates only.
"""

import re

from pydantic import BaseModel

from opspod.skills.base import Skill

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
CARD_FRAGMENT = re.compile(r"(?i)\b(?:ending(?: in)?|last 4)\s*\d{4}\b")
CARD_FULL = re.compile(r"\b(?:\d[ -]?){13,19}\b")
PHONE = re.compile(r"(?<![\w-])\+?\d[\d\s().-]{7,}\d(?!\w)")
ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Boulevard|Blvd)\b"
)


class RedactInput(BaseModel):
    text: str
    names: list[str] = []  # known rider/driver names to scrub


class RedactOutput(BaseModel):
    redacted: str
    mapping: dict[str, str]  # token -> original; never sent to a model or logged
    fields_sent: dict[str, int]  # category -> count, safe to log


def _redact(inp: RedactInput) -> RedactOutput:
    text = inp.text
    mapping: dict[str, str] = {}
    counts: dict[str, int] = {}

    def sub(pattern: re.Pattern[str], label: str) -> None:
        nonlocal text

        def repl(m: re.Match[str]) -> str:
            counts[label] = counts.get(label, 0) + 1
            token = f"[{label}_{counts[label]}]"
            mapping[token] = m.group(0)
            return token

        text = pattern.sub(repl, text)

    sub(EMAIL, "EMAIL")
    sub(CARD_FRAGMENT, "CARD")
    sub(CARD_FULL, "CARD")
    sub(PHONE, "PHONE")
    sub(ADDRESS, "ADDRESS")
    for i, name in enumerate(inp.names, 1):
        token = f"[RIDER_{i}]"
        # Full name first, then each name part, so "Asha" alone is also scrubbed.
        for part in sorted({name, *name.split()}, key=len, reverse=True):
            if len(part) < 3:
                continue
            pat = re.compile(rf"\b{re.escape(part)}\b", re.IGNORECASE)
            if pat.search(text):
                counts["NAME"] = counts.get("NAME", 0) + 1
                mapping[token] = name
                text = pat.sub(token, text)
    return RedactOutput(redacted=text, mapping=mapping, fields_sent=counts)


def rehydrate(text: str, mapping: dict[str, str]) -> str:
    for token, original in mapping.items():
        text = text.replace(token, original)
    return text


SKILL = Skill("redact_pii", "1.0.0", RedactInput, RedactOutput, _redact)
