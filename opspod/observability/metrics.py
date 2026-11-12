"""Prometheus metrics. Labels stay low-cardinality (no case ids, no PII)."""

from prometheus_client import Counter, Gauge, Histogram

from opspod.llm.gateway import CallEvent

CASES = Counter("opspod_cases_total", "Cases finished by pipeline", ["status", "route", "path"])
PIPELINE_ERRORS = Counter("opspod_pipeline_errors_total", "Pipeline step failures", ["step"])
STEP_SECONDS = Histogram("opspod_pipeline_step_seconds", "Step duration", ["step"])
CASE_E2E_SECONDS = Histogram(
    "opspod_case_e2e_seconds",
    "Intake-to-decision time including queue wait (the SLO measure; excludes human wait)",
    buckets=(0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, 900),
)
QUEUE_DEPTH = Gauge("opspod_queue_depth", "Jobs waiting in the pipeline queue")
QUEUE_OLDEST_AGE = Gauge(
    "opspod_queue_oldest_age_seconds", "Age of the oldest job waiting in the pipeline queue"
)
CASE_SECONDS = Histogram(
    "opspod_case_duration_seconds",
    "Intake-to-decision processing time (excludes human wait)",
    buckets=(0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120),
)
LLM_CALLS = Counter(
    "opspod_llm_calls_total", "LLM provider attempts", ["provider", "model", "outcome"]
)
LLM_LATENCY = Histogram("opspod_llm_latency_seconds", "LLM call latency", ["provider"])
LLM_TOKENS = Counter("opspod_llm_tokens_total", "LLM tokens", ["provider", "direction"])
LLM_QUEUE_WAIT = Histogram(
    "opspod_llm_queue_wait_seconds",
    "Time spent waiting for a provider rate-limit slot",
    ["provider"],
    buckets=(0.01, 0.1, 0.5, 1, 2, 5, 10, 30),
)
LLM_REPAIRS = Counter("opspod_llm_repairs_total", "LLM outputs that needed a repair retry")
LLM_FALLBACKS = Counter("opspod_llm_fallback_total", "Results served by a fallback model")
LLM_BUDGET_NEAR = Gauge("opspod_llm_budget_near_limit", "1 when a provider budget is >=90% used")
POLICY_BLOCKED = Counter(
    "opspod_policy_violation_blocked_total", "Proposals/actions blocked by policy", ["rule"]
)
IDEMPOTENT_REPLAYS = Counter(
    "opspod_idempotent_replays_total", "Duplicate money actions absorbed by idempotency keys"
)
REVERSALS = Counter("opspod_reversals_total", "Executed reversals")
APPROVAL_WAIT = Histogram(
    "opspod_approval_wait_seconds",
    "Time from approval request to human decision",
    buckets=(10, 60, 300, 900, 3600, 14400, 86400),
)
APPROVAL_QUEUE = Gauge("opspod_approval_queue_depth", "Pending approvals")
DLQ_DEPTH = Gauge("opspod_dlq_depth", "Unresolved dead letters")
DLQ_TOTAL = Counter("opspod_dlq_total", "Cases dead-lettered")
INTAKE = Counter("opspod_intake_total", "Intake requests", ["result"])  # created|duplicate


def llm_event_sink(e: CallEvent) -> None:
    """Gateway CallEvent -> metrics."""
    LLM_CALLS.labels(e.provider, e.model, e.outcome).inc()
    if e.queue_wait_s:
        LLM_QUEUE_WAIT.labels(e.provider).observe(e.queue_wait_s)
    if e.outcome in ("ok", "invalid"):
        LLM_LATENCY.labels(e.provider).observe(e.latency_s)
        LLM_TOKENS.labels(e.provider, "in").inc(e.tokens_in)
        LLM_TOKENS.labels(e.provider, "out").inc(e.tokens_out)
