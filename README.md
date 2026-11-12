# OpsPod

An agentic system that resolves payment and refund disputes for a fictional ride-and-delivery
marketplace (MoveCo). **The LLM proposes; a deterministic policy engine disposes.** It is built to
be safe, observable, measurable and operable, not to show off a clever agent loop.

Everything is synthetic: no real payments, no real PII. See [`PRD.md`](PRD.md) for scope and
[`docs/architecture.md`](docs/architecture.md) for the design.

## What it does

- **Intake** (`POST /v1/cases`, idempotent) -> **redact PII** -> **context** -> **rule triage**
  (exact money math, duplicate detection, fraud signals) -> **LLM only for ambiguous cases** ->
  **policy engine** -> auto-execute, ask a human, or escalate.
- **Safe by construction.** The model has no tools and never produces an amount. Money moves only
  through a scoped, capped, idempotent tool registry into an append-only ledger. Every failure
  routes to a human, never to an automatic payment.
- **Reversible and auditable.** Refunds are ledger entries; reversals are compensating entries.
  Every decision is replayable from its audit record.
- **Operable.** Traces (case id = trace id), metrics, alert rules with unit tests, a runbook, a
  kill switch, staged rollout modes, and failure injection with a test per failure.

## Results

Measured by `make eval` on the dev split (158 cases: 135 generated + 23 hand-written). Full report,
per-tag pass rates and the release gate: [`EVALS.md`](EVALS.md).

| Metric | Result | Target |
|---|---|---|
| Resolution correctness | 100% | >= 90% |
| Policy compliance, safety subset (63 cases) | 100% | 100% |
| Adversarial/fraud escalation recall | 100% | >= 95% |
| Tool-selection accuracy | 100% | >= 95% |
| Injection cases that caused an action | 0 | 0 |
| Case latency p95 (stub LLM) | 3.3 s | < 30 s |
| Auto-resolve rate | 55% | >= 60% (stretch) |

The holdout split (55 cases) was run once as a release check and also passed the gate. A
deliberately regressed prompt is blocked by the gate (a confident always-refund prompt:
correctness -7.6 points; a malformed-output prompt: schema-valid rate 0%).

> **Read this before quoting the numbers.** The LLM in these runs is a fixed, label-blind *stub*,
> because no provider keys were available to record real-model cassettes. The numbers prove the
> pipeline, the policy engine and the safety layers. They say **nothing yet about real
> free-tier model quality**; that needs `make probe` and `make eval-live` with real keys. The
> gate then replays the recorded cassettes at zero cost. Perfect dev scores also mean the dev set
> is easy for a rules-first system; the holdout and hand-written boundary cases are the
> better evidence.

Other measured evidence:

- Safety/chaos: 400+ tests, including property tests for money math and ledger invariants,
  concurrent-duplicate payment tests, crash-and-resume tests, and one chaos test per failure mode.
- Rollout simulation (shadow -> assisted -> canary -> auto): [`docs/rollout.md`](docs/rollout.md).
- Injected incident, diagnosed from this system's own telemetry:
  [`docs/postmortems/PM-001.md`](docs/postmortems/PM-001.md).

## Quickstart

Requires Python 3.12 (via [uv](https://docs.astral.sh/uv/)), Docker, and `make`.

```bash
uv sync
cp .env.example .env        # add OPENROUTER_API_KEY / GEMINI_API_KEY for probe and live evals
make up                     # postgres, redis, jaeger, prometheus, alertmanager, grafana
make migrate
make seed                   # deterministic synthetic dataset -> evals/datasets/
make lint test              # ruff + mypy, then unit + integration tests (needs `make up`)
make eval                   # golden-set eval + release gate
make chaos                  # failure-injection suite
make alerts-test            # promtool unit tests for the alert rules
```

Run it locally (a stub LLM is used when `LLM_MODE=fake`, the default):

```bash
make dev                    # API on :8000, console at /console/login
make worker                 # pipeline worker (metrics on :9101)
uv run python scripts/load.py --rate 1 --duration 60     # send golden cases
```

Dev API keys (`dev-analyst`, `dev-approver`, `dev-admin`, `dev-agent`) are the defaults of the
`API_KEYS` setting in `opspod/core/config.py`; they are placeholders, not real auth.

Grafana `:3000` (dashboard "OpsPod"), Jaeger `:16686`, Prometheus `:9090`.

Ports 5433 (Postgres) and 6381 (Redis) are used on the host to avoid clashing with other local
databases.

## Layout

| Path | What |
|---|---|
| `opspod/policy` | Pure policy engine and YAML loader (3 regions with different rules) |
| `opspod/ledger` | Idempotent payouts, compensating reversals, append-only tables |
| `opspod/tools` | Scoped, capped tool registry: the only door to side effects |
| `opspod/skills` | Typed, versioned skills (redaction, refund math, fraud, injection, ...) |
| `opspod/llm` | Gateway: routing, repair, fallback, limiter, breaker, budget, cassettes |
| `opspod/pipeline` | State machine, worker, approvals, replay |
| `opspod/evals`, `scripts/run_eval.py` | Harness, metrics, release gate |
| `docs/` | Architecture, runbook, SLOs, security, rollout, discovery, postmortem |

## Honest status

Built and tested: everything above. Not built or not yet measured:

- Real-model quality (no provider keys here): `make probe`, `make eval-live`.
- Production auth (API keys are a placeholder), real data integrations (context is
  fixture-backed behind a `ContextRepo` protocol), the PRD stretch items (second workflow, MCP
  server).
- The synthetic domain tables from the PRD (riders, trips, ...) are not separate DB tables.
