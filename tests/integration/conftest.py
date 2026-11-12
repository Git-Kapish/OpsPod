"""Integration fixtures: real Postgres (docker compose), fresh `opspod_test` schema per session.

Skips locally when the DB is down; fails in CI (CI=true) so a missing service can't hide.
"""

import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from opspod.core.config import get_settings
from opspod.db.models import Case

ROOT = Path(__file__).resolve().parents[2]
BASE_URL = get_settings().database_url
TEST_URL = BASE_URL.rsplit("/", 1)[0] + "/opspod_test"


async def _reset_test_db() -> None:
    admin = create_async_engine(BASE_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with admin.connect() as c:
        row = await c.execute(text("select 1 from pg_database where datname='opspod_test'"))
        if row.scalar() is None:
            await c.execute(text("create database opspod_test"))
    await admin.dispose()
    eng = create_async_engine(TEST_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    async with eng.connect() as c:
        await c.execute(text("drop schema public cascade"))
        await c.execute(text("create schema public"))
    await eng.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_db() -> None:
    try:
        asyncio.run(_reset_test_db())
    except (OSError, asyncpg.PostgresError) as exc:
        if os.getenv("CI"):
            raise
        pytest.skip(f"postgres not reachable (run `make up`): {exc}", allow_module_level=False)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "opspod" / "db" / "migrations"))
    cfg.attributes["url"] = TEST_URL
    command.upgrade(cfg, "head")


@pytest_asyncio.fixture
async def sessionmaker_():
    engine = create_async_engine(TEST_URL, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def session(sessionmaker_) -> AsyncSession:
    async with sessionmaker_() as s:
        yield s


async def make_case(sm) -> uuid.UUID:  # type: ignore[no-untyped-def]
    cid = uuid.uuid4()
    async with sm() as s, s.begin():
        s.add(Case(id=cid, external_id=f"ext-{cid}", type="duplicate_charge", region="us"))
    return cid
