"""Shared fixtures. DB-backed tests use a real Postgres (the compose `db` service locally,
a service container in CI) because the cache relies on Postgres-specific upserts."""

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db import models  # noqa: F401  (registers tables on Base.metadata)
from app.db.base import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5433/wallet_risk_test",
)


async def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    try:
        conn = await asyncpg.connect(
            host=parsed.host,
            port=parsed.port,
            user=parsed.username,
            password=parsed.password,
            database="postgres",
        )
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.fail(
            f"Postgres not reachable for DB tests ({parsed.host}:{parsed.port}): {exc}. "
            "Run `docker compose up -d db` or set TEST_DATABASE_URL."
        )
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", parsed.database
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{parsed.database}"')
    finally:
        await conn.close()


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    await _ensure_database(TEST_DATABASE_URL)
    engine = create_async_engine(TEST_DATABASE_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
