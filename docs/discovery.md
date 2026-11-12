# Discovery (draft)

Simulated interviews with MoveCo support-ops. Fictional company, synthetic data. Status: first draft, Phase 0.

## Current workflow (as-is)
1. Customer files a dispute (app form or webhook).
2. Analyst opens trip tool, payments tool, customer-history tool, merchant tool, policy wiki.
3. Analyst decides outcome, computes amount by hand, issues refund in the billing tool.
4. Analyst writes a reply to the customer.

## Pain points
- 4-5 tools per case; ~8-12 minutes of mostly mechanical lookups.
- Inconsistent decisions across regions and shifts.
- Manual money math causes over/under refunds.
- Fraud (repeat disputers, coached claims) is spotted by memory, not signals.

## Where each approach fits
| Step | Approach | Reason |
|---|---|---|
| Duplicate-charge detection | Deterministic | Exact match on payment records |
| Refund amount calculation | Deterministic | Money math must be exact |
| Policy limits, region rules | Deterministic (YAML) | Auditable, versioned |
| Reading free-text complaint, judging ambiguity, summarizing evidence | LLM | Unstructured language |
| Fraud pattern flagging | Deterministic signals + LLM explanation | Signals decide; LLM only explains |
| Approve high-value refund | Human | Consequential |
| Customer message wording | LLM within strict template | Tone only, no commitments |

## Risks
- Prompt injection via complaint text -> LLM never calls money tools; policy engine is final authority.
- Free-tier LLMs are weak/rate-limited -> deterministic first, gateway with fallbacks, human on failure.
- Duplicate webhooks -> idempotency keys on every money action.
- Provider may train on prompts -> synthetic data + PII redaction only.

## Open questions
- Real per-region limits are invented placeholders until Phase 1 policy YAML.
