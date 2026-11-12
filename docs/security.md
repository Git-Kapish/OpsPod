# Security and data handling

Everything in this repository is synthetic: a fictional company, invented people, no real
payments, no real PII. The controls below are built as if the data were real.

## Provider data statement

The LLM providers are free tiers (OpenRouter `:free` models, Google AI Studio). **Free-tier
providers may log prompts and use them for training.** This is the reason all data is synthetic and
all PII is redacted before any LLM call. Do not point this system at real customer data on a free
tier.

## Controls (PRD S1-S10) and where they live

| Control | Implementation | Test |
|---|---|---|
| S1 permissions | Tool registry: each tool declares a scope; callers are `Principal`s with scopes and caps (`opspod/tools/registry.py`) | `tests/integration/test_tools.py` |
| S2 spending limits | Per-case and per-day caps (tool layer, defense in depth), per-customer refund frequency (policy), all from `config/policies/*.yaml` | `test_tools.py`, `test_policy.py` |
| S3 approvals | Only `approver`/`admin` may decide; who/when/why recorded; edited amount can only reduce | `test_approvals.py` |
| S4 idempotency | `UNIQUE(idempotency_key)` on `actions`, key `(case, kind, version)`; concurrent duplicates pay once | `test_ledger.py`, `test_approvals.py::concurrent` |
| S5 reversibility | Reversal is a new linked ledger entry; ledger and audit rows cannot be updated or deleted (Postgres triggers, also blocks TRUNCATE) | `test_ledger.py::test_append_only` |
| S6 audit | Append-only `audit_log`: input hash, context, prompt version, model, raw model output, proposal, rule ids, action, actor, trace id. `GET /v1/cases/{id}/replay` re-derives the decision from the record alone | `test_approvals.py::replay` |
| S7 data minimization | `redact_pii` removes names, emails, phones, addresses and card fragments before any LLM call; the mapping stays server-side; the sent fields are logged by category only | `test_skills.py`, `test_pipeline.py::test_llm_never_sees_pii` |
| S8 prompt injection | Detection before the LLM; case text wrapped as untrusted data; the model has no tool access and returns a schema-validated proposal; its own output is scanned for override instructions; the policy engine decides routes | `tests/chaos`, `test_eval_gate.py` |
| S9 escalation | Low confidence, conflicting evidence, fraud signals, repeat disputes or any validation failure routes to a human with a prepared summary | `test_pipeline.py` |
| S10 fail safe | Any exception, timeout, invalid output or unknown state routes to a human or the DLQ; never auto-execute | `tests/chaos` |

## Trust boundaries

```
 untrusted                       |  semi-trusted            |  trusted
 customer complaint text         |  LLM output (proposal)   |  policy engine, ledger,
 webhook payloads                |  free-tier provider      |  tool registry, DB
 ---------------------------------------------------------------------------------
 detect_injection + redact_pii -> LLM -> schema validation -> policy engine -> tools
```

The model never sees raw PII and never holds a tool. Its output is data that the policy engine may
reject. Money amounts are computed in code; the model only chooses among a fixed set of bases.

## Secrets

API keys come from the environment only (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`); `.env` is
git-ignored; `.env.example` has blanks. Keys are held as `SecretStr` and sent in headers, never in
URLs (the Gemini key goes in `x-goog-api-key`). Logs carry ids and hashes, not complaint text.

## Known limitations (honest list)

- **Auth is a placeholder.** API keys map to roles via the `API_KEYS` setting; the defaults are dev
  keys (`dev-admin`, ...). Replace before any shared deployment. There is no user management and no
  rate limiting on the API.
- The console stores the key in a `HttpOnly`, `SameSite=Strict` cookie; it has no CSRF tokens
  beyond that, and no session expiry.
- The console loads htmx from a CDN with a pinned version and an SRI hash.
- `POST /internal/alerts` needs the alerts key; the default is a dev key.
- Chaos injection is honoured only when `APP_ENV=dev`.
- Context (trips, payments, history) comes from a fixture-backed repository, not production
  systems; the `ContextRepo` protocol is where a real integration plugs in.
- No real-model quality numbers exist yet (see `EVALS.md`).
