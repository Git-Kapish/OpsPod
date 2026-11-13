RUN = uv run

.DEFAULT_GOAL := help
.PHONY: help up down migrate seed dev worker lint test chaos eval eval-live eval-holdout \
        alerts-test dashboards rollout probe

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

up: ## start postgres, redis, jaeger, prometheus, alertmanager, grafana
	docker compose up -d
down: ## stop the local stack
	docker compose down
migrate: ## apply database migrations
	$(RUN) alembic upgrade head
seed: ## regenerate the deterministic golden dataset
	$(RUN) python scripts/generate_dataset.py
dev: ## run the API with reload (http://localhost:8000, console at /console/login)
	$(RUN) uvicorn opspod.api.main:app --reload
worker: ## run the pipeline worker (metrics on :9101)
	$(RUN) arq opspod.pipeline.worker.WorkerSettings

lint: ## ruff + mypy
	$(RUN) ruff check . && $(RUN) ruff format --check . && $(RUN) mypy opspod
test: ## unit + integration + chaos tests (needs `make up`; never calls live providers)
	LLM_MODE=fake $(RUN) pytest
chaos: ## failure-injection suite only
	LLM_MODE=fake $(RUN) pytest tests/chaos

eval: ## golden-set eval + release gate (dev split; replays recorded cassettes, else stub LLM)
	$(RUN) python scripts/run_eval.py
eval-holdout: ## release check on the holdout split (run sparingly; never tune on it)
	$(RUN) python scripts/run_eval.py --split holdout --no-report
eval-live: ## live provider eval; spends free-tier quota and records new cassettes
	$(RUN) python scripts/run_eval.py --live
probe: ## probe free providers; writes config/llm.probe.yaml and docs/provider-probe.md
	$(RUN) python scripts/probe_providers.py

alerts-test: ## promtool: validate and unit-test the Prometheus alert rules (needs docker)
	MSYS_NO_PATHCONV=1 docker run --rm --entrypoint promtool -v "$(CURDIR)/infra:/infra" prom/prometheus:v2.53.0 check rules /infra/alerts.yml
	MSYS_NO_PATHCONV=1 docker run --rm --entrypoint promtool -v "$(CURDIR)/infra:/infra" prom/prometheus:v2.53.0 test rules /infra/alerts_test.yml
dashboards: ## regenerate the Grafana dashboard JSON
	$(RUN) python scripts/make_dashboard.py
rollout: ## shadow -> assisted -> canary -> auto simulation; regenerates docs/rollout.md (slow)
	$(RUN) python scripts/rollout_sim.py
