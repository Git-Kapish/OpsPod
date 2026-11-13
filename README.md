# OpsPod

An agentic system that resolves payment and refund disputes for a fictional ride-and-delivery
marketplace (MoveCo). **The LLM proposes; a deterministic policy engine disposes.** It is built to
be safe, observable, measurable and operable, not to show off a clever agent loop.

Everything is synthetic: no real payments, no real PII. See [`PRD.md`](PRD.md) for scope and
[`docs/architecture.md`](docs/architecture.md) for the design.

![Console and Grafana demo](docs/demo.gif)

*Approval queue, approve action, case page with audit and replay check, dashboards, and the Grafana view of the injected incident.*

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

Real model: **`nvidia/nemotron-3.5-super-vl:free`** via OpenRouter (free tier), recorded to
cassettes with `make eval-live` and replayed free in CI by `make eval`. Full report, per-tag pass
rates and the release gate: [`EVALS.md`](EVALS.md).

| Metric | Dev (158 cases) | Holdout (55 cases, run once) | Target |
|---|---|---|---|
| Resolution correctness | 92.4% | 90.9% | >= 90% |
| Route accuracy | 98.1% | 98.2% | |
| Policy compliance, safety subset | 100% | 100% | 100% |
| Adversarial/fraud escalation recall | 100% | 100% | >= 95% |
| Tool-selection accuracy | 98.1% | 98.2% | >= 95% |
| Injection cases that caused an action | 0 | 0 | 0 |
| Schema-valid LLM output | 100% (12/12 calls) | 100% (5/5 calls) | >= 99% |
| Case latency p95 | 4.6 s | 6.3 s | < 30 s |
| Auto-resolve rate | 55% | | >= 60% (stretch) |

**What the model gets wrong, and what it doesn't.** Every miss is an *ambiguous* case (the only
kind that reaches the LLM, about 8% of traffic). Deterministic cases, fraud, injection and boundary
cases are 100% on dev. On ambiguous cases the model usually proposes the full rule amount (or a
credit) where the synthetic label expects a half split, so it scores 0/12 on dev and 1/6 on holdout
for those. The label ("mixed fault means half") is my convention, not ground truth, so read this as
"the model does not follow that convention", not "the model is wrong". Safety is unaffected: LLM
proposals always go to a human for review (`llm_proposals_auto: false`), and on dev the model escalated three
ambiguous cases instead (the safe direction). Latency is dominated by the model: a reasoning
model, about 10 s and about 1,000 completion tokens per call, on about 8% of cases.

Rule-only (stub-LLM) runs are separate and still gate CI changes to non-LLM code: 100% on dev and
holdout. A deliberately regressed prompt is blocked by the gate (an always-refund prompt:
correctness -7.6 points; a malformed-output prompt: schema-valid rate 0%).

> Caveats. 12 + 5 real calls is a small sample; the per-tag figures above are not statistically
> strong. Replay latency is cassette speed, so latency figures come from the live runs. Dev
> was visible while building the prompt and holdout was run once, but with a 55-case set a few
> cases move the percentage a lot.

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

Run it locally (a stub LLM is used when `LLM_MODE=fake`, the default; put your
`OPENROUTER_API_KEY` in `.env.local`, which is git-ignored, and set `LLM_MODE=live` for real calls):

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

- Real-model quality beyond one free model on a small set (see Results): only
  `nvidia/nemotron-3.5-super-vl:free` was run, and the Gemini provider is unit-tested but unexercised.
- Production auth (API keys are a placeholder), real data integrations (context is
  fixture-backed behind a `ContextRepo` protocol), the PRD stretch items (second workflow, MCP
  server).
- The synthetic domain tables from the PRD (riders, trips, ...) are not separate DB tables.
