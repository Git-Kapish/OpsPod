import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from opspod.core.config import get_settings
from opspod.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return str(config.attributes.get("url") or get_settings().database_url)


def _run(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def _run_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_run)
    await engine.dispose()


asyncio.run(_run_online())
