RUN = uv run

.PHONY: alerts-test dashboards up down migrate seed dev worker test lint eval eval-live probe chaos

up:
	docker compose up -d
down:
	docker compose down
migrate:
	$(RUN) alembic upgrade head
seed:
	$(RUN) python scripts/generate_dataset.py
dev:
	$(RUN) uvicorn opspod.api.main:app --reload
worker:
	$(RUN) arq opspod.pipeline.worker.WorkerSettings
test:
	LLM_MODE=fake $(RUN) pytest
lint:
	$(RUN) ruff check . && $(RUN) ruff format --check . && $(RUN) mypy opspod
eval:
	$(RUN) python scripts/run_eval.py
eval-live:
	$(RUN) python scripts/run_eval.py --live
probe:
	$(RUN) python scripts/probe_providers.py
chaos:
	LLM_MODE=fake $(RUN) pytest tests/chaos

alerts-test:
	MSYS_NO_PATHCONV=1 docker run --rm --entrypoint promtool -v "$(CURDIR)/infra:/infra" prom/prometheus:v2.53.0 check rules /infra/alerts.yml
	MSYS_NO_PATHCONV=1 docker run --rm --entrypoint promtool -v "$(CURDIR)/infra:/infra" prom/prometheus:v2.53.0 test rules /infra/alerts_test.yml
dashboards:
	$(RUN) python scripts/make_dashboard.py
