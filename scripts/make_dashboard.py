"""Generate the Grafana dashboard JSON (kept as code so panels stay consistent)."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DS = {"type": "prometheus", "uid": "prometheus"}

# (title, unit, [(expr, legend)])
PANELS = [
    (
        "Cases by route (rate/min)",
        "short",
        [("sum by (route) (rate(opspod_cases_total[5m])) * 60", "{{route}}")],
    ),
    (
        "Auto-resolve rate",
        "percentunit",
        [
            (
                'sum(rate(opspod_cases_total{route="AUTO_EXECUTE",status="resolved"}[15m])) / sum(rate(opspod_cases_total[15m]))',
                "auto",
            )
        ],
    ),
    (
        "Escalation rate",
        "percentunit",
        [
            (
                'sum(rate(opspod_cases_total{route="ESCALATE"}[15m])) / sum(rate(opspod_cases_total[15m]))',
                "escalated",
            )
        ],
    ),
    (
        "Cases by outcome status",
        "short",
        [("sum by (status) (rate(opspod_cases_total[5m])) * 60", "{{status}}")],
    ),
    (
        "Case latency p50/p95 (s)",
        "s",
        [
            (
                "histogram_quantile(0.5, sum(rate(opspod_case_duration_seconds_bucket[5m])) by (le))",
                "p50",
            ),
            (
                "histogram_quantile(0.95, sum(rate(opspod_case_duration_seconds_bucket[5m])) by (le))",
                "p95",
            ),
        ],
    ),
    (
        "Step latency p95 (s)",
        "s",
        [
            (
                "histogram_quantile(0.95, sum(rate(opspod_pipeline_step_seconds_bucket[5m])) by (le, step))",
                "{{step}}",
            )
        ],
    ),
    (
        "LLM calls by outcome",
        "short",
        [("sum by (outcome) (rate(opspod_llm_calls_total[5m])) * 60", "{{outcome}}")],
    ),
    (
        "LLM latency p95 (s)",
        "s",
        [
            (
                "histogram_quantile(0.95, sum(rate(opspod_llm_latency_seconds_bucket[5m])) by (le, provider))",
                "{{provider}}",
            )
        ],
    ),
    (
        "LLM error / 429 rate",
        "percentunit",
        [
            (
                'sum(rate(opspod_llm_calls_total{outcome!~"ok|cache_hit"}[5m])) / sum(rate(opspod_llm_calls_total{outcome!="cache_hit"}[5m]))',
                "errors",
            ),
            (
                'sum(rate(opspod_llm_calls_total{outcome="rate_limited"}[5m])) / sum(rate(opspod_llm_calls_total[5m]))',
                "429",
            ),
        ],
    ),
    (
        "Fallback and repair usage (per min)",
        "short",
        [
            ("sum(rate(opspod_llm_fallback_total[5m])) * 60", "fallback"),
            ("sum(rate(opspod_llm_repairs_total[5m])) * 60", "repair"),
        ],
    ),
    (
        "Token usage (per min)",
        "short",
        [("sum by (direction) (rate(opspod_llm_tokens_total[5m])) * 60", "{{direction}}")],
    ),
    ("Approval queue depth", "short", [("opspod_approval_queue_depth", "pending")]),
    (
        "Approval wait p95 (s)",
        "s",
        [
            (
                "histogram_quantile(0.95, sum(rate(opspod_approval_wait_seconds_bucket[1h])) by (le))",
                "p95",
            )
        ],
    ),
    ("Reversals (per hour)", "short", [("increase(opspod_reversals_total[1h])", "reversals")]),
    (
        "Policy violations blocked (5m)",
        "short",
        [("sum by (rule) (increase(opspod_policy_violation_blocked_total[5m]))", "{{rule}}")],
    ),
    (
        "Idempotent replays (5m)",
        "short",
        [("increase(opspod_idempotent_replays_total[5m])", "replays")],
    ),
    ("Dead letter depth", "short", [("opspod_dlq_depth", "unresolved")]),
    (
        "Pipeline step errors (5m)",
        "short",
        [("sum by (step) (increase(opspod_pipeline_errors_total[5m]))", "{{step}}")],
    ),
]


def panel(i: int, title: str, unit: str, targets: list[tuple[str, str]]) -> dict:
    return {
        "id": i + 1,
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": 7, "w": 8, "x": (i % 3) * 8, "y": (i // 3) * 7},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "targets": [
            {"refId": chr(65 + n), "expr": expr, "legendFormat": legend, "datasource": DS}
            for n, (expr, legend) in enumerate(targets)
        ],
    }


dashboard = {
    "title": "OpsPod",
    "uid": "opspod-main",
    "schemaVersion": 39,
    "version": 1,
    "refresh": "10s",
    "time": {"from": "now-1h", "to": "now"},
    "panels": [panel(i, *p) for i, p in enumerate(PANELS)],
}

if __name__ == "__main__":
    out = ROOT / "infra" / "grafana" / "dashboards" / "opspod.json"
    out.write_text(json.dumps(dashboard, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(PANELS)} panels)")
