"""Test fixtures.

Everything runs against a real PostgreSQL 16. Never SQLite: none of the triggers, none of
the isolation behaviour and none of the concurrency semantics this project is about exist
there, so a green SQLite suite would prove nothing.

If DATABASE_URL is set (CI, where Postgres is a service container) it is used directly.
Otherwise a throwaway container is started via testcontainers.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

ROOT = Path(__file__).resolve().parents[1]

TRUNCATE_ALL = (
    "TRUNCATE TABLE balance_snapshots, entries, transactions, accounts RESTART IDENTITY CASCADE"
)


def _run_migrations(url: str) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(config, "head")


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    from ledger.config import settings

    url = os.environ.get("DATABASE_URL")
    container: Any = None

    if not url:
        from testcontainers.postgres import PostgresContainer

        container = PostgresContainer("postgres:16-alpine", driver="asyncpg")
        container.start()
        url = container.get_connection_url()

    # alembic/env.py reads the URL from this same settings singleton.
    settings.database_url = url
    os.environ["DATABASE_URL"] = url
    _run_migrations(url)

    try:
        yield url
    finally:
        if container is not None:
            container.stop()


@pytest_asyncio.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    # The concurrency tests fan out to 50 simultaneous requests, each of which needs its
    # own connection, so the pool has to be bigger than SQLAlchemy's default of 5.
    created = create_async_engine(database_url, pool_size=60, max_overflow=20, pool_pre_ping=True)
    async with created.begin() as connection:
        await connection.execute(text(TRUNCATE_ALL))
    try:
        yield created
    finally:
        await created.dispose()


@pytest_asyncio.fixture
async def sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@pytest_asyncio.fixture
async def session(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with sessions() as opened:
        yield opened


@pytest_asyncio.fixture
async def client(
    sessions: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    """An HTTP client where every request gets its own database session.

    That is what makes the concurrency tests real: two requests issued with asyncio.gather
    genuinely run on two connections, rather than sharing one and serialising by accident.
    """
    from ledger.db import get_session
    from ledger.main import app

    async def _session_override() -> AsyncIterator[AsyncSession]:
        async with sessions() as opened:
            yield opened

    app.dependency_overrides[get_session] = _session_override
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://ledger.test") as opened_client:
        yield opened_client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def make_account(
    client: AsyncClient,
    name: str = "account",
    *,
    currency: str = "AUD",
    allow_negative: bool = False,
) -> dict[str, Any]:
    response = await client.post(
        "/accounts",
        json={"name": name, "currency": currency, "allow_negative": allow_negative},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def transfer(
    client: AsyncClient,
    source: str,
    destination: str,
    amount_minor: int,
    *,
    currency: str = "AUD",
    description: str | None = None,
    idempotency_key: str | None = None,
) -> Any:
    return await client.post(
        "/transactions",
        headers={"Idempotency-Key": idempotency_key or str(uuid4())},
        json={
            "description": description,
            "postings": [
                {"account_id": source, "amount_minor": -amount_minor, "currency": currency},
                {"account_id": destination, "amount_minor": amount_minor, "currency": currency},
            ],
        },
    )


async def fund(client: AsyncClient, account_id: str, amount_minor: int) -> str:
    """Credit an account from a freshly created account that is allowed to go negative."""
    source = await make_account(client, "funding source", allow_negative=True)
    response = await transfer(client, source["id"], account_id, amount_minor)
    assert response.status_code == 201, response.text
    return str(source["id"])


async def balance_of(client: AsyncClient, account_id: str, *, as_of: str | None = None) -> int:
    params = {"as_of": as_of} if as_of else None
    response = await client.get(f"/accounts/{account_id}/balance", params=params)
    assert response.status_code == 200, response.text
    return int(response.json()["balance_minor"])
