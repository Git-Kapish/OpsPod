"""Production entrypoint: `uvicorn opspod.api.main:app`. Enqueues cases to the arq worker."""

import time

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from opspod.api.app import create_app
from opspod.core.config import get_settings
from opspod.core.logging import configure_logging
from opspod.observability.tracing import configure_exporter
from opspod.pipeline.bootstrap import build_deps

settings = get_settings()
configure_logging()
configure_exporter(settings.otel_exporter_otlp_endpoint)
_pool = None


async def _enqueue(case_id: str, force: bool = False) -> None:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    job_id = f"{case_id}-retry-{int(time.time())}" if force else case_id
    await _pool.enqueue_job("process_case", case_id, _job_id=job_id)


async def _queue_stats() -> tuple[int, float]:
    """Depth and oldest-job age of the arq queue (score = enqueue time in ms)."""
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    depth = int(await _pool.zcard("arq:queue"))
    oldest = await _pool.zrange("arq:queue", 0, 0, withscores=True)
    age = max(0.0, time.time() - oldest[0][1] / 1000) if oldest else 0.0
    return depth, age


app: FastAPI = create_app(build_deps(settings), _enqueue, queue_stats=_queue_stats)
