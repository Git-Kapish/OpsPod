"""Stub LLM behaviours for deterministic evals. They read only the prompt, never the labels.

`good` is a neutral, sane stand-in. `bad_refund` and `malformed` simulate a regressed prompt or
model, used to prove the release gate blocks a bad change. None of these measure real-model
quality: that needs recorded cassettes from live providers (see docs/provider-notes.md).
"""

import json
import re
from collections.abc import Callable

StubFn = Callable[[str, str], str]


def _analysis(user: str, outcome: str, basis: str, conf: float, rationale: str) -> str:
    ctype = re.search(r"Case type: (\w+)", user)
    return json.dumps(
        {
            "case_type": ctype.group(1) if ctype and ctype.group(1) != "unknown" else "wrong_fare",
            "outcome": outcome,
            "amount_basis": basis,
            "confidence": conf,
            "rationale": rationale,
            "conflicting_evidence": False,
            "instructions_in_text": False,
        }
    )


def good(system: str, user: str) -> str:
    if "amount_basis" in system:
        m = re.search(r"Rule calculation: .*?amount (\d+)", user)
        if m and int(m.group(1)) == 0:
            return _analysis(user, "DENY", "none", 0.8, "Rule calculation found nothing owed.")
        return _analysis(user, "PARTIAL_REFUND", "half_calculated", 0.85, "Mixed causes; split.")
    if '"sentence"' in system:
        return json.dumps({"sentence": "Thanks for your patience while we looked into this."})
    return json.dumps({"case_type": "wrong_fare", "confidence": 0.5, "instructions_in_text": False})


def bad_refund(system: str, user: str) -> str:
    """A regressed prompt that always proposes a confident full refund."""
    if "amount_basis" in system:
        return _analysis(user, "FULL_REFUND", "disputed_full", 0.99, "Customer is right.")
    return good(system, user)


def malformed(system: str, user: str) -> str:
    """A regressed prompt whose output is no longer valid JSON."""
    return (
        "Sure! Here is my answer: refund them." if "amount_basis" in system else good(system, user)
    )


STUBS: dict[str, StubFn] = {"good": good, "bad_refund": bad_refund, "malformed": malformed}
