# OpsPod

Agentic dispute-resolution system for a fictional ride-and-delivery marketplace (MoveCo). The LLM proposes; a deterministic policy engine disposes. Synthetic data only.

See `PRD.md` for scope and `docs/implementation-plan.md` for the build plan.

## Quickstart
```
uv sync
cp .env.example .env     # add OPENROUTER_API_KEY / GEMINI_API_KEY for `make probe`
make up                  # postgres, redis, jaeger, prometheus, grafana
make seed                # deterministic synthetic dataset -> evals/datasets/
make lint test
```

## Status
Phases 0-1 done (foundations; domain, ledger, policy engine). Results and eval numbers will be added as they are measured.
