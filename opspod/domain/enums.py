from enum import StrEnum


class CaseType(StrEnum):
    WRONG_FARE = "wrong_fare"
    DUPLICATE_CHARGE = "duplicate_charge"
    MISSING_ITEM = "missing_item"
    LATE_CANCEL_FEE = "late_cancel_fee"
    NO_SHOW = "no_show"
    PROMO_NOT_APPLIED = "promo_not_applied"
    SUSPICIOUS = "suspicious"


class Outcome(StrEnum):
    FULL_REFUND = "FULL_REFUND"
    PARTIAL_REFUND = "PARTIAL_REFUND"
    CREDIT = "CREDIT"
    DENY = "DENY"
    REQUEST_INFO = "REQUEST_INFO"
    ESCALATE = "ESCALATE"


MONEY_OUTCOMES = frozenset({Outcome.FULL_REFUND, Outcome.PARTIAL_REFUND, Outcome.CREDIT})


class Route(StrEnum):
    AUTO_EXECUTE = "AUTO_EXECUTE"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    ESCALATE = "ESCALATE"


class RiskTier(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AutonomyMode(StrEnum):
    SHADOW = "SHADOW"
    ASSISTED = "ASSISTED"
    AUTO_LOW_RISK = "AUTO_LOW_RISK"
    KILL_SWITCH = "KILL_SWITCH"


class Role(StrEnum):
    ANALYST = "analyst"
    APPROVER = "approver"
    ADMIN = "admin"
    AGENT_SERVICE = "agent-service"


class LedgerKind(StrEnum):
    REFUND = "refund"
    CREDIT = "credit"
    REVERSAL = "reversal"
