import httpx
import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from opspod.api.app import create_app
from opspod.core.errors import PermissionDenied
from opspod.domain.enums import AutonomyMode
from opspod.observability.tracing import add_exporter
from opspod.pipeline.runner import run_case
from opspod.policy.loader import load_policy
from opspod.tools.registry import ToolContext, agent_principal

from .test_pipeline import BY_TAG, analysis, make_deps, set_mode, submit

pytestmark = pytest.mark.integration
EXPORTER = InMemorySpanExporter()
add_exporter(EXPORTER)


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


async def test_one_trace_per_case_with_step_skill_tool_and_llm_spans(sessionmaker_):
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    deps, _ = make_deps(sessionmaker_)
    clear = next(
        c
        for c in BY_TAG["clear_cut"]
        if c["expected"]["route"] == "auto" and c["expected"]["amount"] > 0
    )
    amb = next(c for c in BY_TAG["ambiguous"] if c["expected"]["outcome"] == "PARTIAL_REFUND")
    for case in (clear, amb):
        EXPORTER.clear()
        cid = await submit(sessionmaker_, case)
        await run_case(deps, cid)
        spans = EXPORTER.get_finished_spans()
        names = {s.name for s in spans}
        assert {s.context.trace_id for s in spans} == {cid.int}  # trace id == case id
        assert {"case.process", "step.triage", "step.policy", "step.execute"} <= names
        assert "skill.calculate_refund" in names and "skill.redact_pii" in names
        if case is clear:
            tool = next(s for s in spans if s.name == "tool.issue_refund")
            assert tool.attributes["tool.scope"] == "refund:write"
        else:
            llm = next(s for s in spans if s.name == "llm.call")
            attrs = llm.attributes
            assert attrs["prompt_version"] == "analyze@v1" and attrs["validation"] == "ok"
            assert (
                attrs["provider"] == "fake" and "tokens_in" in attrs and attrs["retry_count"] == 0
            )


async def test_metrics_move_with_pipeline_activity(sessionmaker_):
    await set_mode(sessionmaker_, AutonomyMode.AUTO_LOW_RISK)
    deps, _ = make_deps(sessionmaker_, script=lambda s, u: analysis())
    before = sample("opspod_cases_total", status="resolved", route="AUTO_EXECUTE", path="rule")
    step_before = sample("opspod_pipeline_step_seconds_count", step="triage")
    case = next(
        c
        for c in BY_TAG["clear_cut"]
        if c["expected"]["route"] == "auto" and c["expected"]["amount"] > 0
    )
    await run_case(deps, await submit(sessionmaker_, case))
    assert (
        sample("opspod_cases_total", status="resolved", route="AUTO_EXECUTE", path="rule")
        == before + 1
    )
    assert sample("opspod_pipeline_step_seconds_count", step="triage") == step_before + 1


async def test_blocked_actions_and_step_errors_are_counted(sessionmaker_):
    from opspod.tools.registry import default_registry

    policy = load_policy("us")
    blocked = sample("opspod_policy_violation_blocked_total", rule="T002_per_case_cap")
    case_id = await submit(sessionmaker_, BY_TAG["clear_cut"][0])
    async with sessionmaker_() as s, s.begin():
        with pytest.raises(PermissionDenied):
            await default_registry().call(
                "issue_refund",
                agent_principal(policy),
                {
                    "case_id": str(case_id),
                    "version": 1,
                    "account": "r",
                    "currency": "USD",
                    "amount": policy.limits.auto_limit + 1,
                },
                ToolContext(s, "us"),
            )
    assert sample("opspod_policy_violation_blocked_total", rule="T002_per_case_cap") == blocked + 1

    deps, _ = make_deps(sessionmaker_)
    errs = sample("opspod_pipeline_errors_total", step="triage")

    def boom(step: str) -> None:
        if step == "triage":
            raise RuntimeError("x")

    deps.before_step = boom
    with pytest.raises(RuntimeError):
        await run_case(deps, case_id)
    assert sample("opspod_pipeline_errors_total", step="triage") == errs + 1


async def test_idempotent_replay_counter(sessionmaker_):
    from opspod.tools.registry import default_registry

    policy = load_policy("us")
    cid = await submit(sessionmaker_, BY_TAG["clear_cut"][0])
    args = {
        "case_id": str(cid),
        "version": 1,
        "account": "rider:dup",
        "amount": 500,
        "currency": "USD",
    }
    before = sample("opspod_idempotent_replays_total")
    for _ in range(3):  # duplicate webhook / retry storm against the same idempotency key
        async with sessionmaker_() as s, s.begin():
            await default_registry().call(
                "issue_refund", agent_principal(policy), args, ToolContext(s, "us")
            )
    assert sample("opspod_idempotent_replays_total") == before + 2


async def test_metrics_endpoint_alert_webhook_and_log_auth(sessionmaker_):
    deps, _ = make_deps(sessionmaker_)

    async def enqueue(_: str) -> None:
        return None

    app = create_app(deps, enqueue)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        m = await c.get("/metrics")
        assert m.status_code == 200 and "opspod_cases_total" in m.text
        assert "opspod_approval_queue_depth" in m.text and "opspod_dlq_depth" in m.text
        hook = {"alerts": [{"status": "firing", "labels": {"alertname": "X", "severity": "page"}}]}
        assert (await c.post("/internal/alerts", json=hook)).status_code == 401
        ok = await c.post(
            "/internal/alerts", json=hook, headers={"Authorization": "Bearer dev-alerts"}
        )
        assert ok.status_code == 204
        bad = await c.post(
            "/internal/alerts", json=hook, headers={"Authorization": "Bearer dev-analyst"}
        )
        assert bad.status_code == 403
