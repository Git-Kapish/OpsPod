# OpsPod — Implementation Plan

## Context
Repo `D:\agentic AI\OpsPod` contains only `PRD.md` and `CLAUDE.md` (not a git repo yet). PRD §16 defines phases 0–9; CLAUDE.md says build one phase at a time, get approval on architecture, and stop after each phase. This request = (1) produce the implementation plan for the whole project, (2) commit and push to https://github.com/Git-Kapish/OpsPod.git.

No code is written in this step. Phase 0 starts only after you approve this plan.

## What I'll do now (on approval)
1. Save this plan as `docs/implementation-plan.md` (repo-visible copy of the plan below).
2. Add a minimal `.gitignore` (Python, `.env`, `.venv`, caches, `evals/results/*.tmp`) — keeps secrets out per PRD §11.2.
3. `git init`, branch `main`, add remote `origin` = the GitHub URL. First check whether the remote already has commits (`git ls-remote`); if it does, fetch and rebase onto it instead of force-pushing.
4. Commits (short, human style, no `feat:` prefix, **no Co-Authored-By / Claude attribution** — your instruction overrides the default attribution lines):
   - `add PRD and project guidelines`  (PRD.md, CLAUDE.md)
   - `add implementation plan`  (docs/implementation-plan.md, .gitignore)
5. `git push -u origin main`. Auth: if push fails on credentials, I'll report and ask you to run `! gh auth login` (or similar) rather than work around it. The GitHub MCP connector is broken (bad auth header), so plain `git`/`gh` is the path.

## Phase plan (each phase ends with a stop-and-report, per CLAUDE.md)

### Phase 0 — Foundations
- Scaffold `opspod/` package layout from CLAUDE.md; `pyproject.toml` (Python 3.12, ruff, mypy strict on core dirs, pytest/pytest-asyncio/hypothesis).
- `Makefile` with all targets listed in CLAUDE.md (`up/down/migrate/seed/dev/worker/test/lint/eval/eval-live/probe/chaos`); unimplemented ones print a clear "phase N" message.
- `docker-compose.yml`: Postgres 16, Redis, Jaeger, Prometheus, Grafana (+ minimal prometheus.yml, grafana provisioning).
- `core/config.py` (pydantic-settings, `.env.example` from PRD §19), `core/errors.py` exception hierarchy, structlog setup.
- CI skeleton `.github/workflows/ci.yml` (lint, mypy, unit; eval gate stubbed).
- `scripts/probe_providers.py` (lists free OpenRouter/Gemini models, tests JSON-schema output, measures latency/429s, writes `config/llm.yaml` + `docs/provider-notes.md`). Needs your API keys at run time only.
- `scripts/generate_dataset.py` seeded RNG → synthetic riders/drivers/trips/orders/payments + first slice of golden cases.
- `docs/discovery.md` first draft (workflow map, agent/rules/human table from PRD §6.3).
- **Acceptance:** `make up` works; probe report written; dataset deterministic (same seed → same hash).
- **Decision needed from you before coding (asked at Phase 0 start):** adding deps/services is gated by CLAUDE.md — I'll list exact dependencies in the Phase 0 plan for sign-off.

### Phase 1 — Domain, ledger, policy engine (safety layer first)
- `domain/`: enums (CaseType, Outcome, Route, AutonomyMode, Role), `Money` (Decimal/minor units, no floats).
- `db/`: SQLAlchemy models per PRD §14 + Alembic initial migration; DB trigger blocking UPDATE/DELETE on `ledger_entries` and `audit_log` (tested).
- `ledger/`: idempotent refund/credit/reverse keyed on `(case_id, action_type, version)`, compensating reversals.
- `policy/`: YAML loader + engine (caps per case/day/customer, allowed outcomes, risk tiers, route decision, rule IDs fired); `config/policies/{us,in,eu}.yaml` with differing rules.
- **Tests:** hypothesis (no double-pay, exact math, reversal sums, non-negative balances); policy ≥95% branch coverage; trigger tests.

### Phase 2 — Skills, tools, registry
- Skills (`fetch_trip_context`, `detect_duplicate_charge`, `calculate_refund`, `redact_pii`, `score_fraud_signals`, `summarize_evidence`, `draft_customer_message`) each with Pydantic in/out contract + version + eval cases. LLM-backed ones call the gateway interface only (stubbed until Phase 3).
- `tools/` registry: name, schema, required scope, idempotency flag, reversibility flag; permission checks; per-case/per-day spend caps.
- **Tests:** out-of-scope call rejected; caps enforced.

### Phase 3 — LLM gateway
- `llm/`: `LLMClient` protocol, OpenRouter / Gemini / Fake / Cassette providers, task routing from `config/llm.yaml`, structured output + one repair retry → fallback → human, token-bucket limiter (Retry-After), circuit breaker, content-hash cache, budget guard, telemetry hooks.
- Prompts in `opspod/prompts/<name>/v<N>.md` + CHANGELOG.
- **Tests (FakeProvider):** 429, timeout, malformed JSON, fallback chain, record/replay.

### Phase 4 — Pipeline, intake, queue
- Typed state machine (hand-rolled; no LangGraph unless trivially thin) with step state persisted in `case_steps`; arq worker; DLQ; `POST /v1/cases` with Idempotency-Key.
- Fail-safe: any error → human route.
- **Tests:** kill worker mid-case → resume; duplicate webhook → one case, one action.

### Phase 5 — Approvals, console, audit, replay
- Approval API + role enforcement (API key + role header), edit-amount, reverse endpoint, append-only audit log capturing all PRD §S6 fields, `/replay`, minimal Jinja2+HTMX console (queue, case detail, dashboard, DLQ).

### Phase 6 — Observability
- OpenTelemetry spans (step/skill/tool/LLM), Prometheus metrics, Grafana dashboards, alert rules, structured logs with case_id = trace_id.
- **Acceptance:** injected failure fires an alert.

### Phase 7 — Evals + CI gate
- ≥150 generated + ~30 hand-written cases, dev/holdout split, tags per PRD §9.1; metrics §9.2; cassette replay in CI; release gate §9.5; `EVALS.md` from real numbers only.
- **Acceptance:** gate blocks a deliberately bad prompt change.

### Phase 8 — Rollout, chaos, incident
- Flags (SHADOW/ASSISTED/AUTO_LOW_RISK/KILL_SWITCH, canary by hash of case id), chaos flags + tests, `docs/runbook.md`, `docs/rollout.md` with shadow agreement report, `docs/postmortems/PM-001.md` from a real injected incident.

### Phase 9 — Polish + stretch
- README with measured results, diagram, demo GIF; second workflow (merchant menu issue) with zero core-engine changes; optional MCP server.

## Cross-cutting rules (from CLAUDE.md, enforced every phase)
LLM proposes / policy disposes; no floats for money; fail-safe to human; ledger+audit append-only; PII redaction before any LLM call; no live providers in tests/CI; no hardcoded model names or limits; strict layering `api → pipeline → skills/tools → ledger/db`; no "Uber"/real brand names; never tune on holdout; honest docs (don't describe unbuilt things as built).

## Risks / open questions
- Free-tier quotas change → probe script + cassettes + budget guard (already in design).
- Windows dev host: Makefile needs `make` (Git Bash/WSL/choco) and Docker Desktop — I'll confirm availability at Phase 0 start.
- Git identity: commits use your existing git config; I'll check `git config user.name/email` first and ask if unset (won't invent one).

## Verification (this step)
- `git log --oneline` shows the two commits with no co-author trailer.
- `git status` clean; `git ls-remote origin` shows `main` at local HEAD.
- Confirm `.env`/keys are ignored by `.gitignore`.

## Status (end of build)

Phases 0-8 are built and tested; Phase 9 polish is done (README with measured results, demo GIF,
diagrams). Not built, by choice: the second workflow (merchant menu issue) and the MCP server.
The policy engine is money-specific, so a second workflow needs a real generalization of the
policy layer, not a bolt-on; the tool registry already exposes `describe()` as the MCP seam.
Real-model quality numbers need provider keys (`make probe`, `make eval-live`).
