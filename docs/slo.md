# SLOs

| SLI | SLO | Measured by | Alert |
|---|---|---|---|
| Case processed without system error | 99.5% | `opspod_pipeline_errors_total` vs `opspod_cases_total` | `PipelineErrorBudgetBurn` |
| p95 latency, intake to decision (includes queue wait, excludes human wait) | < 30 s | `opspod_case_e2e_seconds` histogram (not `opspod_case_duration_seconds`, which starts at worker pickup) | `CaseLatencySLOBurn`, `QueueBacklogAge` |
| Unsafe auto-action rate | 0 | `opspod_policy_violation_blocked_total` (blocked) and the eval safety suite (would-have-paid) | `PolicyViolationBlocked` |
| Approval-queue freshness (item visible after decision) | < 5 s | The approval row is written in the same transaction as the decision, so it is visible on commit | `ApprovalQueueBacklog` (depth, not freshness) |

## How to read them

- "Unsafe auto-action" means money moved outside policy. The safety layers (policy engine, tool
  caps, idempotent ledger) make the blocked counter the leading indicator: any non-zero value
  means a proposal reached a hard rule and was stopped. That is an incident even though no money
  moved.
- Latency excludes human wait by construction: the clock stops at the decision, and approvals are
  a separate histogram (`opspod_approval_wait_seconds`). It **includes queue wait**: an earlier
  version measured from worker pickup and was blind to a 2-minute backlog (PM-001).
- Error budget: 0.5% of cases may hit a pipeline error. A failed step retries, so an error is not
  a lost case; the SLO guards retry pressure and DLQ growth.

## Current measurements

| SLI | Value | Source |
|---|---|---|
| p95 case latency | 3.3 s on 158 dev cases with the stub LLM | `EVALS.md` (no network LLM in that figure) |
| Pipeline crashes | 0 of 158 | `EVALS.md` |
| Unsafe auto-actions | 0 of 63 safety-subset cases | `EVALS.md` |

Latency with real free-tier models has not been measured yet; free tiers are rate limited and
slower, so expect LLM-path cases (about 10% of traffic) to dominate the tail. Re-measure after the
first live `make eval-live` run.
