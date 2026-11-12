"""Summarize key Prometheus series over a time window (incident evidence).

uv run python scripts/prom_snapshot.py --minutes 10
uv run python scripts/prom_snapshot.py --start 2026-11-12T10:00:00Z --end 2026-11-12T10:10:00Z
"""

import argparse
import time
from datetime import datetime

import httpx

QUERIES = {
    "cases/min": "sum(rate(opspod_cases_total[1m])) * 60",
    "escalated share": 'sum(rate(opspod_cases_total{route="ESCALATE"}[2m])) / sum(rate(opspod_cases_total[2m]))',
    "case latency p95 s": "histogram_quantile(0.95, sum(rate(opspod_case_duration_seconds_bucket[2m])) by (le))",
    "llm_analysis step p95 s": 'histogram_quantile(0.95, sum(rate(opspod_pipeline_step_seconds_bucket{step="llm_analysis"}[2m])) by (le))',
    "llm calls/min ok": 'sum(rate(opspod_llm_calls_total{outcome="ok"}[1m])) * 60',
    "llm calls/min 429": 'sum(rate(opspod_llm_calls_total{outcome="rate_limited"}[1m])) * 60',
    "llm calls/min breaker_open": 'sum(rate(opspod_llm_calls_total{outcome="breaker_open"}[1m])) * 60',
    "llm calls/min budget": 'sum(rate(opspod_llm_calls_total{outcome="budget"}[1m])) * 60',
    "fallback results/min": "sum(rate(opspod_llm_fallback_total[1m])) * 60",
    "pipeline errors/min": "sum(rate(opspod_pipeline_errors_total[1m])) * 60",
    "approval queue depth": "opspod_approval_queue_depth",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:9090")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--step", default="15s")
    args = ap.parse_args()
    end = (
        datetime.fromisoformat(args.end.replace("Z", "+00:00")).timestamp()
        if args.end
        else time.time()
    )
    start = (
        datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp()
        if args.start
        else end - args.minutes * 60
    )
    print("| series | min | avg | max |\n|---|---|---|---|")
    for name, expr in QUERIES.items():
        r = httpx.get(
            f"{args.url}/api/v1/query_range",
            params={"query": expr, "start": start, "end": end, "step": args.step},
            timeout=30,
        ).json()
        vals = [float(v) for s in r["data"]["result"] for _, v in s["values"] if v != "NaN"]
        if vals:
            print(f"| {name} | {min(vals):.3g} | {sum(vals) / len(vals):.3g} | {max(vals):.3g} |")
        else:
            print(f"| {name} | - | - | - |")


if __name__ == "__main__":
    main()
