"""Exception hierarchy. Anything not caught as OpsPodError is treated as fail-safe -> human."""


class OpsPodError(Exception):
    """Base for all expected application errors."""


class ConfigError(OpsPodError):
    pass


class PolicyError(OpsPodError):
    pass


class PolicyViolation(PolicyError):
    """A proposal or action exceeds policy; must be blocked and escalated."""


class LedgerError(OpsPodError):
    pass


class PermissionDenied(OpsPodError):
    pass


class LLMError(OpsPodError):
    pass


class LLMTimeout(LLMError):
    pass


class LLMRateLimited(LLMError):
    def __init__(self, message: str = "rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMInvalidOutput(LLMError):
    pass


class LLMUnavailable(LLMError):
    """All providers failed, circuit open, or budget exhausted."""
