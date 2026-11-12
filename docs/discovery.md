# Discovery

Simulated discovery for MoveCo support operations. MoveCo is fictional and the interviews below
are written to mirror what a real discovery pass would surface; they are not field research.

## Interviews (simulated)

**Ops analyst, 3 years.** "I open the trip tool, the payments tool, the customer history, then the
policy wiki. Half my day is checking the same three facts: was the driver actually there, was the
customer charged twice, is this their fourth refund this month. The math is the part I get wrong
when I am tired. Rules differ by country and I keep the exceptions in my head."

**Ops lead.** "Two analysts decide the same case differently. I cannot tell the CFO how much we
refund by region without a spreadsheet. I do not want anything that can refund more than a person
could, and I want to see why it decided. Fraud is maybe 3% of cases but it is most of the
money we lose."

**Finance.** "Every refund must be reversible and traceable to a decision. If something goes
wrong I need to stop it in one click and know what it already paid."

## Workflow today (as-is)

1. Customer files a dispute (app form or webhook).
2. Analyst opens trip, payments, customer-history, merchant tools and the policy wiki.
3. Analyst decides the outcome and computes the amount by hand.
4. Analyst issues the refund in the billing tool and writes the customer reply.

## Pain points

- 4-5 tools per case; 8-12 minutes of mostly mechanical lookups.
- Inconsistent decisions across analysts and regions.
- Manual money math causes over- and under-refunds.
- Fraud is spotted by memory, not signals.
- No audit trail linking a refund to the reasoning behind it.

## Where each approach fits

| Step | Approach | Reason |
|---|---|---|
| Duplicate-charge detection | Deterministic | Exact match on payment records |
| Refund amount calculation | Deterministic | Money math must be exact |
| Policy limits, region rules | Deterministic (YAML) | Auditable, versioned |
| Reading a free-text complaint, judging ambiguity, summarizing evidence | LLM | Unstructured language |
| Fraud pattern flagging | Deterministic signals, LLM may explain | Signals decide; the model never does |
| Approving a high-value refund | Human | Consequential |
| Customer message wording | LLM inside a strict template | Tone only, no commitments |

The consequence for the build: about 90% of cases are fully rule-decidable, so the LLM is used
only on the ambiguous minority (and never for numbers). That keeps LLM calls per case low, which
also protects free-tier quotas.

## Case types in scope

Wrong fare, duplicate charge, missing or wrong item, late-cancellation fee, driver no-show, promo
not applied, and suspicious/out-of-policy cases (always escalated).

## Risks and how the design answers them

| Risk | Answer |
|---|---|
| Prompt injection in complaint text | Detection before the LLM, untrusted-data wrapping, no tool access for the model, policy engine decides |
| Over-refunding | Caps in the policy engine and again in the tool layer; amounts computed in code |
| Duplicate webhooks and retries | Idempotency keys with a UNIQUE constraint; per-case lock |
| Free-tier models are weak and rate limited | Deterministic first; schema validation, repair retry, fallback, then humans |
| Providers may train on prompts | Synthetic data only and PII redaction before every call |
| Hard to roll back | Shadow, assisted, canary, auto; kill switch; compensating-entry reversals |

## Open questions (for a real engagement)

- Real per-region limits: the numbers in `config/policies/*.yaml` are invented placeholders.
- How should partial fault be handled when both driver and app are at fault? The pipeline currently
  sends these to review.
- What is the acceptable reversal rate before a canary is rolled back?
