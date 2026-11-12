"""Production entrypoint: `uvicorn opspod.api.main:app`. Enqueues cases to the arq worker."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI

from opspod.api.app import create_app
from opspod.core.config import get_settings
from opspod.core.logging import configure_logging
from opspod.pipeline.bootstrap import build_deps

settings = get_settings()
configure_logging()
_pool = None


async def _enqueue(case_id: str) -> None:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    await _pool.enqueue_job("process_case", case_id, _job_id=case_id)


app: FastAPI = create_app(build_deps(settings), _enqueue)
__all__ = ["app", "asynccontextmanager", "AsyncIterator"]
