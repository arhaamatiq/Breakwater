"""Shared fixtures for the Postgres integration tests.

The session creates a throwaway database `bw_test_<random>` on the server named
by BW_TEST_DSN (default: the docker-compose Postgres), applies the packaged
schema, and drops the database at teardown. If that server is unreachable the
integration tests are skipped, so the unit tests still run anywhere:

    docker compose up -d postgres          # container breakwater-postgres, :55432
    uv run pytest -q
"""
import asyncio
import os
import uuid
from urllib.parse import urlparse, urlunparse

import asyncpg
import pytest

from breakwater import db

TEST_DSN = os.environ.get(
    "BW_TEST_DSN", "postgresql://breakwater:breakwater@127.0.0.1:55432/breakwater")


def dsn_for(database: str) -> str:
    return urlunparse(urlparse(TEST_DSN)._replace(path="/" + database))


async def admin_connect():
    return await asyncpg.connect(TEST_DSN, timeout=3)


async def create_database(name: str, schema: str | None = None) -> str:
    """Create `name`; apply `schema` (SQL text) or, by default, the packaged schema."""
    conn = await admin_connect()
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    conn = await asyncpg.connect(dsn_for(name))
    try:
        if schema is None:
            await db.apply_schema(conn)
        else:
            await conn.execute(schema)
    finally:
        await conn.close()
    return dsn_for(name)


async def drop_database(name: str) -> None:
    conn = await admin_connect()
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def pg_dsn():
    async def reachable():
        conn = await admin_connect()
        await conn.close()
    try:
        asyncio.run(reachable())
    except (OSError, asyncio.TimeoutError, asyncpg.CannotConnectNowError) as e:
        pytest.skip(f"Postgres unreachable at BW_TEST_DSN={TEST_DSN} ({type(e).__name__}); "
                    f"start it with `docker compose up -d postgres`")
    name = f"bw_test_{uuid.uuid4().hex[:10]}"
    dsn = asyncio.run(create_database(name))
    yield dsn
    asyncio.run(drop_database(name))


@pytest.fixture
def pg(pg_dsn):
    """Empty tables per test: claims are global across experiments, so leftovers
    from one test would be claimed by the next."""
    async def truncate():
        conn = await asyncpg.connect(pg_dsn)
        try:
            await conn.execute(
                "TRUNCATE results, trials, experiments, events, workers RESTART IDENTITY CASCADE")
        finally:
            await conn.close()
    asyncio.run(truncate())
    return pg_dsn
