# Eval results

Generated 2026-11-12 11:00 UTC - split `dev` - 158 cases (63 in the safety subset).

**Stub LLM (`good`)**: a fixed, label-blind stand-in. These numbers measure the pipeline, policy engine and safety layers. They do **not** measure real model quality.

## Release gate: PASS

All gate conditions met.

## Metrics vs targets

| Metric | Result | Target | Met |
|---|---|---|---|
| Resolution correctness | 100.0% | >= 90% | yes |
| Policy compliance (safety subset) | 100.0% | 100% | yes |
| Adversarial/fraud escalation recall | 100.0% | >= 95% | yes |
| Tool-selection accuracy | 100.0% | >= 95% | yes |
| Injection cases causing an action | 0 | 0 | yes |
| Schema-valid LLM output | 100.0% | >= 99% | yes |
| Case latency p95 (s) | 0.16 | < 30 | yes |
| Auto-resolve rate (stretch) | 55.1% | >= 60% | NO |

The golden set over-represents adversarial, ambiguous and high-value cases on purpose, so the auto-resolve rate here is not a traffic forecast.

## Other metrics

| Metric | Value |
|---|---|
| route_accuracy | 100.0% |
| parameter_accuracy | 100.0% |
| case_pass_rate | 100.0% |
| policy_compliance_all | 100.0% |
| escalation_precision | 100.0% |
| escalation_recall | 100.0% |
| auto_resolve_rate | 55.1% |
| hallucinated_tool_calls | 0 |
| llm_calls_per_case | 0.08 |
| tokens_per_case | 1.52 |
| latency_p50_s | 0.12 |
| errors | 0 |

## Pass rate by tag

| Tag | Cases | Pass rate |
|---|---|---|
| ambiguous | 12 | 100% |
| boundary | 4 | 100% |
| clear_cut | 44 | 100% |
| conflicting_evidence | 14 | 100% |
| duplicate_webhook | 10 | 100% |
| edge | 10 | 100% |
| fraud | 25 | 100% |
| handwritten | 23 | 100% |
| high_value | 18 | 100% |
| injection | 5 | 100% |
| messy_text | 2 | 100% |
| missing_data | 18 | 100% |
| pii | 1 | 100% |
| region_eu | 45 | 100% |
| region_in | 40 | 100% |
| region_us | 73 | 100% |
| repeat_disputes | 24 | 100% |
| social_engineering | 1 | 100% |

## Failing cases (0)

None.

## Regressions vs baseline

None.

Cost: free tier. LLM calls this run: 12.
