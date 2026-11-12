RUN = uv run

.PHONY: up down migrate seed dev worker test lint eval eval-live probe chaos

up:
	docker compose up -d
down:
	docker compose down
migrate:
	$(RUN) alembic upgrade head
seed:
	$(RUN) python scripts/generate_dataset.py
dev:
	$(RUN) uvicorn opspod.api.app:app --reload
worker:
	$(RUN) arq opspod.pipeline.worker.WorkerSettings
test:
	LLM_MODE=fake $(RUN) pytest -m "not integration"
lint:
	$(RUN) ruff check . && $(RUN) ruff format --check . && $(RUN) mypy opspod
eval:
	LLM_MODE=replay $(RUN) python scripts/run_eval.py
eval-live:
	LLM_MODE=live $(RUN) python scripts/run_eval.py --live
probe:
	$(RUN) python scripts/probe_providers.py
chaos:
	LLM_MODE=fake $(RUN) pytest tests/chaos
