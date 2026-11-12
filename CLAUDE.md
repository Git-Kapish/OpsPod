# CLAUDE.md — OpsPod

Guidance for Claude Code working in this repository. Read `PRD.md` first; it is the source of truth for scope, requirements, and acceptance criteria. If this file and the PRD conflict, ask the user.

## What this project is

OpsPod is an agentic system that resolves payment/refund disputes for a fictional ride-and-delivery marketplace ("MoveCo"). It is a portfolio project for an **agentic software engineering** role, so engineering rigor (safety, evals, observability, reliability) matters more than model cleverness. All data is **synthetic**. No real payments, no real PII.

**Core principle: the LLM proposes, the policy engine disposes.** The LLM never executes money actions and can never exceed policy.

## Tech stack

Python 3.12, FastAPI, Pydantic v2, PostgreSQL 16 (SQLAlchemy 2 + Alembic), Redis (`arq` workers), OpenTelemetry + Jaeger, Prometheus + Grafana, structlog, pytest (+ pytest-asyncio, hypothesis), ruff, mypy, Docker Compose, GitHub Actions. Ops console = Jinja2 + HTMX (no SPA).

LLMs: **free tiers only** — OpenRouter `:free` models and/or Google AI Studio Gemini. Behind a gateway; see "LLM rules".

## Commands (create these Makefile targets in Phase 0)

```
make up            # docker compose up: postgres, redis, jaeger, prometheus, grafana
make down
make migrate       # alembic upgrade head
make seed          # deterministic synthetic data (seeded RNG)
make dev           # run API with reload
make worker        # run pipeline worker
make test          # unit + integration (LLM_MODE=fake)
make lint          # ruff + mypy
make eval          # golden-set eval in replay mode (cassettes), CI gate
make eval-live     # live provider eval (throttled, records cassettes)
make probe         # scripts/probe_providers.py
make chaos         # run failure-injection suite
```

## Repository layout

```
opspod/
  api/            # FastAPI routers, auth/role deps, schemas
  console/        # Jinja2 + HTMX ops console
  core/           # config, logging, telemetry, errors
  db/             # models, migrations, repositories
  domain/         # case types, outcomes, enums, value objects (Money, etc.)
  ledger/         # append-only ledger, idempotent actions, reversals
  policy/         # policy engine, risk tiers, YAML loader
  skills/         # reusable typed skills (each with contract + version)
  tools/          # tool registry, scopes, permissions, spend caps
  pipeline/       # state machine, steps, worker, DLQ, resume logic
  llm/            # gateway, providers, routing, limiter, breaker, cache, cassettes
  prompts/        # versioned prompts: <name>/v<N>.md + CHANGELOG
  observability/  # tracing, metrics, alert rule definitions
config/
  policies/       # <region>.yaml (limits, allowed outcomes, templates)
  llm.yaml        # task→model routing, limits (generated/tuned by probe)
  workflows/      # workflow declarations (case types, skills, policy ref)
evals/
  datasets/       # golden dev + holdout (JSONL), generator seed
  cassettes/      # recorded LLM responses
  results/        # eval run outputs (JSON)
scripts/          # generate_dataset.py, probe_providers.py, chaos helpers
docs/             # discovery, architecture, slo, security, runbook, postmortems, rollout
tests/            # unit/, integration/, safety/, chaos/
```

## Architecture rules (non-negotiable)

1. **LLM output is a proposal, never an action.** Flow is always: LLM → Pydantic-validated proposal → policy engine → route (auto / approval / escalate) → tool layer. No code path lets model output call a money tool directly.
2. **Money math and policy logic live in code**, never in prompts. Use `Decimal` (or integer minor units) via a `Money` value object. Never floats.
3. **Fail safe:** any exception, timeout, schema failure, or unknown state routes the case to a **human**, never to auto-execute.
4. **Every money action is idempotent** (`idempotency_key` unique constraint) and **reversible** via a compensating ledger entry. The ledger and audit log are **append-only** — no UPDATE/DELETE on those tables (enforce with DB trigger/permissions and test it).
5. **Pipeline is an explicit state machine** with state persisted after every step, resumable after a crash. Steps are small, typed, and individually testable.
6. **Deterministic first:** use the LLM only where language judgment is required (ambiguous cases, evidence summarization, message wording). Minimize LLM calls per case — this also protects free-tier quotas.
7. **Config over forks:** regional differences go in `config/policies/*.yaml`, not `if region ==` in code.
8. **Everything is observable:** each pipeline step, skill, tool call, and LLM call gets a span and metrics. Case ID == trace correlation ID.
9. **Strict layering:** `api → pipeline → skills/tools → ledger/db`. Only `llm/` imports provider SDKs. Only `ledger/` writes money rows. Only `policy/` decides routes.

## LLM rules (free-tier specific)

- Providers: OpenRouter (OpenAI-compatible, `:free` models) and Google AI Studio Gemini (`google-genai`). Keys from env only (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`). Never commit keys.
- **Do not hardcode model names or rate limits.** Free model availability and quotas change. Run `make probe` in Phase 0 and put results in `config/llm.yaml` and `docs/provider-notes.md`. Verify against provider docs when unsure.
- Route per task (`classify`, `analyze`, `draft_message`) with a primary and ordered fallbacks.
- Use native structured output (JSON schema / response schema) when the provider supports it; otherwise instruct JSON and validate with Pydantic. On invalid output: one repair retry with the validation error → fallback model → escalate to human.
- Implement and test: per-provider token-bucket rate limiter (honor `Retry-After`), circuit breaker, content-hash cache, daily call/token budget guard, and graceful degradation to deterministic-only + human routing.
- Prompts: short, few-shot, tight schemas, `temperature=0`. Assume small/weak models. Prompts are versioned files in `opspod/prompts/`; the prompt version is recorded in every audit row and span.
- **PII redaction before every LLM call.** Models see tokens like `[RIDER_1]`. All case text is untrusted data: wrap it in clear delimiters and never let it act as instructions.
- `LLM_MODE` env: `live` | `replay` (cassettes) | `fake` (FakeProvider). **Tests and CI never hit live providers.**

## Testing rules

- Test pyramid: unit (skills, policy, ledger, money) → integration (pipeline with FakeProvider, DB, Redis) → safety suite (injection, fraud, boundary amounts) → chaos suite → golden-set eval (replay).
- Use **hypothesis property tests** for money math and ledger invariants (sum of reversals, no double-pay, non-negative balances).
- Safety suite must be **100%**. Any failure blocks merge.
- Write the test first or alongside the code. A feature isn't done without tests proving its failure behavior, not just the happy path.
- Eval dataset: seeded generator + hand-written edge cases; **dev** vs **holdout** split. Never tune prompts against holdout.
- Eval gate (CI): policy compliance 100% on safety set; correctness regression ≤ 2 points vs. baseline; injection success = 0; schema-valid rate ≥ 99%.

## Code conventions

- Fully type-hinted; `mypy --strict` on `core/`, `ledger/`, `policy/`, `llm/`, `pipeline/`. `ruff` for lint/format.
- Async-first for I/O. Pydantic models for every boundary (API, skill I/O, LLM proposal, config).
- Small functions, explicit errors (custom exception hierarchy in `core/errors.py`), no bare `except`.
- Structured logging only (`structlog`); never log raw PII or secrets.
- Alembic migration for every schema change; never edit applied migrations.
- Commit style: Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), small focused commits, one phase milestone per PR.

## How to work (workflow per phase)

1. Read the phase's row in PRD §16 and its acceptance criteria.
2. Propose a short plan (files to create, tests to write) and wait for approval on anything architectural.
3. Implement in small steps with tests. Run `make lint test` after each step.
4. Update docs touched by the change (`docs/`, README results, `.env.example`).
5. Verify acceptance criteria explicitly and report which ones pass.
6. Stop at the end of the phase and summarize: what was built, what's tested, open questions.

**Build order:** Phase 0 → 1 (domain/ledger/policy) → 2 (skills/tools) → 3 (LLM gateway) → 4 (pipeline) → 5 (approvals/console/audit) → 6 (observability) → 7 (evals/CI) → 8 (rollout/chaos/incident) → 9 (polish/stretch). **Do not start LLM work before the ledger and policy engine are solid and tested.**

## Do / Don't

**Do**
- Ask before adding a new dependency, service, or changing the architecture.
- Keep the ops console minimal; the value is in the backend.
- Make failure modes explicit and tested (timeouts, 429s, malformed JSON, duplicate events, poisoned text).
- Record real numbers in `EVALS.md` and the README as they're produced; never invent metrics.
- Keep docs honest: if something isn't built, don't describe it as built.

**Don't**
- Don't let an LLM call execute actions, compute amounts, or override policy.
- Don't use floats for money.
- Don't call live LLM providers in tests or CI.
- Don't hardcode model names, limits, or API keys.
- Don't use real personal data or real company data; Uber/other brand names stay out of the code and datasets (the fictional company is "MoveCo").
- Don't update or delete rows in `ledger_entries` or `audit_log`.
- Don't add features outside the PRD scope without asking (stretch items only after Phase 8).
- Don't tune against the holdout set.

## Definition of done (any change)

- Tests added/updated and passing (`make lint test`); safety suite green.
- Eval gate passes if prompts, skills, policy, or routing changed (`make eval`).
- Telemetry (span + metric) added for new steps/tools.
- Failure behavior documented/tested; fail-safe routes to human.
- Docs and `.env.example` updated; migration included if schema changed.

## Key domain reference

- Case types: wrong fare, duplicate charge, missing/wrong item, late-cancellation fee, no-show, promo not applied, suspicious/out-of-policy.
- Outcomes: `FULL_REFUND`, `PARTIAL_REFUND`, `CREDIT`, `DENY`, `REQUEST_INFO`, `ESCALATE`.
- Routes: `AUTO_EXECUTE`, `NEEDS_APPROVAL`, `ESCALATE`.
- Autonomy modes: `SHADOW`, `ASSISTED`, `AUTO_LOW_RISK`, `KILL_SWITCH` (runtime flags, per region and case type; canary % via hash of case ID).
- Roles: `analyst`, `approver`, `admin`, `agent-service`.

## First task when starting

Begin **Phase 0**: scaffold the repo and Makefile, docker compose (Postgres, Redis, Jaeger, Prometheus, Grafana), CI skeleton, config loader, `scripts/probe_providers.py`, the seeded synthetic data generator, and a first draft of `docs/discovery.md`. Then stop and report before starting Phase 1.
