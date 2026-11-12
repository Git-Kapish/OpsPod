"""detect_injection: flag text that tries to instruct the system (S8). Detection only escalates."""

import re

from pydantic import BaseModel

from opspod.skills.base import Skill

PATTERNS = [
    r"ignore\s+(?:all\s+|any\s+)?(?:previous|prior|above)\s+(?:instructions|rules)",
    r"\bsystem\s*:",
    r"you\s+are\s+now\b",
    r"\badmin\s+mode\b",
    r"\b(?:approve|refund)\s+(?:any|everything|immediately)",
    r"skip\s+(?:the\s+)?(?:approval|checks?|policy)",
    r"override\s+(?:the\s+)?policy",
    r"</?\s*(?:case|system|instructions?)\s*>",
    r"\bnew\s+policy\b",
    r"\bdisregard\b.{0,30}\b(?:rules|policy|instructions)\b",
]
COMPILED = [re.compile(p, re.IGNORECASE) for p in PATTERNS]


class InjectionInput(BaseModel):
    text: str


class InjectionOutput(BaseModel):
    detected: bool
    matched: list[str]


def _detect(inp: InjectionInput) -> InjectionOutput:
    hits = [p.pattern for p in COMPILED if p.search(inp.text)]
    return InjectionOutput(detected=bool(hits), matched=hits)


SKILL = Skill("detect_injection", "1.0.0", InjectionInput, InjectionOutput, _detect)
