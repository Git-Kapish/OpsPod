# Runbook

On-call guide. Every Prometheus alert links to a section here. Dashboards: Grafana "OpsPod"
(`localhost:3000`), traces: Jaeger (`localhost:16686`, search by case id: the trace id is the
case id without dashes), alerts: Prometheus `:9090/alerts`, Alertmanager `:9093`.

## First five minutes, for any page

1. **Is money at risk?** If `PolicyViolationBlocked` fired or reversals are spiking, stop the line
   first: `PUT /v1/admin/flags {"kill_switch": true}` (admin key). Every case goes to humans and
   LLM calls stop, mid-case included. Investigate second.
2. **Scope it.** Grafana panels: cases by route/status, LLM calls by outcome, step errors, DLQ depth.
3. **Pick one failing case** and open `/console/cases/{id}`: timeline, audit events, replay check.
4. **Mitigate with the smallest switch** (table below), then fix the cause, then step the mode back up.

## Mitigation switches

| Switch | How | Effect |
|---|---|---|
| Kill switch | `PUT /v1/admin/flags {"kill_switch": true}` | All cases to humans immediately; LLM disabled; no auto-execution |
| Mode downgrade | `PUT /v1/admin/flags {"autonomy": {"default": "ASSISTED", "canary_percent": 0}}` | Auto -> approval for everything; per region/case type overrides supported (`"us:wrong_fare": "SHADOW"`) |
| Shrink canary | `canary_percent` in the same flag | Only that share of low-risk cases auto-execute |
| Switch provider | edit `config/llm.yaml` task chains (primary first), restart API and worker | New primary; fallbacks follow in order |
| Drain DLQ | fix the cause, then `POST /v1/admin/dlq/{id}/retry` (console shows the DLQ) | Case resumes from its last completed step; ledger idempotency prevents double pay |
| Reverse a refund | `POST /v1/actions/{id}/reverse` (approver) or the Reverse button on the case page | Compensating ledger entry linked to the original; never an update or delete |

Autonomy order of safety: `KILL_SWITCH` > `ASSISTED` > `SHADOW` (no action) > `AUTO_LOW_RISK`.
Whenever in doubt, go down one step.

## Alerts

### LLM error rate
`LLMErrorRateHigh`: more than 30% of provider attempts failing for 2 minutes.
- Diagnose: Grafana "LLM calls by outcome" (timeout vs rate_limited vs error vs invalid). Jaeger:
  `llm.call` spans with `error` attribute, `fallback_index`, `retry_count`.
- Impact: ambiguous cases fail safe to humans (path `llm_unavailable`); clear-cut cases are
  unaffected (no LLM). Nothing is paid wrongly.
- Mitigate: if one provider is down, the breaker opens by itself after 5 consecutive failures and
  traffic moves to the fallback. If all providers are down, the system degrades to
  deterministic-only plus human routing; consider `ASSISTED` so humans are not surprised.

### LLM rate limit storm
`LLMRateLimitStorm`: sustained provider 429s.
- Diagnose: `opspod_llm_calls_total{outcome=~"rate_limited|breaker_open"}` by provider (once the
  breaker trips it hides the raw 429s); is one provider responsible? Compare with the provider's limits in `docs/provider-notes.md`.
- Mitigate: lower `requests_per_minute` for that provider in `config/llm.yaml`, or move it to the
  end of the chain; reduce ambiguous volume by shrinking the canary. See `docs/postmortems/PM-001.md`
  for a worked example of this incident.

### LLM fallback rate
`LLMFallbackRateHigh`: more than half the results come from fallback models.
- The primary is degraded but not down. Check primary latency/429 panels. Fallback models are
  usually weaker: watch the schema-valid rate and the repair rate (`opspod_llm_repairs_total`).
- Mitigate: fix or replace the primary; if fallbacks produce more invalid output, prefer
  `ASSISTED` for the affected case types.

### LLM budget
`LLMBudgetNearLimit`: a provider is at 90% of its daily call budget.
- At 100% the gateway stops calling that provider (`budget` outcome) and cases degrade to humans.
- Mitigate: raise `daily_calls` only if the free tier allows it; otherwise reduce LLM usage
  (shrink canary, keep `llm_tone` off).

### Escalation anomaly
`EscalationRateAnomaly`: more than 60% of cases escalating.
- Diagnose: by rule id in the `policy.decision` audit events and `opspod_policy_violation_blocked_total`.
  Common causes: provider outage (`llm_unavailable`), a bad policy YAML edit (limits too low),
  an injection campaign (`P009`), fraud ring (`P006`/`P005`).
- Mitigate: if self-inflicted (config), revert the config; if an attack, leave escalating.

### Approval backlog
`ApprovalQueueBacklog`: more than 50 pending approvals for 10 minutes.
- Diagnose: console queue; are approvers online? Did a mode downgrade route everything to approval?
- Mitigate: add approvers; once the cause of the downgrade is fixed, raise the mode back.

### Policy violation blocked
`PolicyViolationBlocked` (page): a proposal or tool call hit a hard policy rule (`P001-P004`,
`P011`) or a tool-layer cap (`T001-T003`, `X001`).
- The block worked: nothing was paid. But a proposal should never get this far, so find out why.
- Diagnose: `rule` label on the metric; audit `policy.decision` and `action.refused` events for the
  case; check the LLM raw output in `llm.proposal`. Run replay: `GET /v1/cases/{id}/replay`.
- Mitigate: a prompt or model regression is the usual cause; downgrade to `ASSISTED`, then run
  `make eval` against the previous prompt pin (`config/prompts.yaml`).

### Duplicate execution
`DuplicateExecutionSpike`: many duplicate money actions absorbed by idempotency keys.
- Idempotency keys worked (nothing paid twice) but something is redelivering: webhook source
  retrying, worker redelivery after timeouts, or the requeue sweeper looping.
- Diagnose: `opspod_intake_total{result="duplicate"}`, worker logs for `CaseLocked`/`busy`.
- A *real* double payment would show as two payout actions for one `(case, kind, version)`, which
  the unique constraint makes impossible; verify with
  `select idempotency_key, count(*) from actions group by 1 having count(*) > 1;` (expect 0 rows).

### Latency SLO
`CaseLatencySLOBurn`: p95 intake-to-decision latency above 30s. The metric
(`opspod_case_e2e_seconds`) starts at intake, so it **includes time spent waiting in the queue**;
`opspod_case_duration_seconds` (processing only) does not, and can look healthy during a backlog
(see PM-001).
- Diagnose: queue depth/age panel first (backlog?), then step latency (which step), LLM queue wait
  and LLM latency. Jaeger: look at where the time goes in a slow trace.
- Mitigate: slow LLM provider -> reorder chain or reduce load; slow DB -> check Postgres; backlog
  -> see Queue backlog.

### Queue backlog
`QueueBacklogAge` (page): the oldest queued case has waited more than 30s.
- Workers are not keeping up. Check the worker count/`max_jobs`, then what the workers are busy
  with: slow LLM calls, rate-limit waits, slow DB, or a flood of intake.
- Deterministic cases share workers with LLM cases, so an LLM problem can delay everything. The
  gateway bounds rate-limit waits (`queue_wait_s`, default 5s) so this should stay short; if it
  does not, suspect the DB or a stuck provider call.
- Mitigate: stop LLM usage (`KILL_SWITCH` routes cases to humans without LLM calls) to free the
  workers; add workers; shrink the canary to cut intake of LLM-path cases.

### LLM load shedding
`LLMLoadShedding`: the gateway is refusing to queue for a provider rate-limit slot, so excess
LLM-path cases go to the next provider or to humans. This is the designed overload behavior, and a
sign that demand exceeds the providers' free-tier limits.
- Check `opspod_llm_queue_wait_seconds` and `opspod_llm_calls_total{outcome="shed"}` by provider.
- Mitigate: raise `requests_per_minute` only to the provider's real limit; add capacity on the
  fallback (it is usually sized smaller than the primary); shrink the canary.

### Pipeline errors
`PipelineErrorBudgetBurn`: step failures threaten the 99.5% success SLO.
- Diagnose: `opspod_pipeline_errors_total{step}`; audit `step.failed` events carry the error type.
- Failed steps retry with backoff, then dead-letter. Cases are never lost and never auto-executed
  on failure.

### Dead letters
`DeadLetterNotEmpty`: a case exhausted its retries.
- Diagnose: console `/console/dlq` (error, attempts), then the case page.
- Fix the cause, then retry (`POST /v1/admin/dlq/{id}/retry`) or mark resolved after handling the
  case manually.

## Failure injection (dev only)

`POST /v1/admin/chaos` (admin, `APP_ENV=dev`; every toggle is audited). Flags live in the DB so the
API and worker agree. Each has a test in `tests/chaos/` and a guaranteed behavior:

| Flag | Injected failure | Expected behavior |
|---|---|---|
| `llm_timeout` | provider calls time out | Retried with backoff, then fallback, then ambiguous cases escalate to humans; breaker opens after 5 failures; clear-cut cases unaffected |
| `llm_429` | provider returns 429 with Retry-After | Limiter penalized, backoff honors Retry-After, same escalation path; alert `LLMRateLimitStorm` |
| `llm_malformed` | provider returns non-JSON | One repair retry, then fallback, then escalate; never a guess |
| `llm_provider` | scope the `llm_*` flags to one provider | Exercises fallback to the other provider |
| `tool_500` | tool layer raises unexpectedly | Step fails and retries; nothing paid; DLQ after 3 tries; after recovery the case resumes and pays exactly once |
| `slow_db_ms` | latency added before every step | Cases slow down, still complete correctly; latency SLO alert |
| `duplicate_webhook` | intake enqueues each case 3 times | One case, one action; extra deliveries back off (`CaseLocked`) |
| `poisoned_text` | injection text appended at intake | Escalated (`P009`), no LLM call, no money |

Run all of them: `make chaos`. Reset: `POST /v1/admin/chaos {}`.
