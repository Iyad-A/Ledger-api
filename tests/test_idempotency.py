"""Idempotency-Key behaviour, including the concurrent case."""

from __future__ import annotations

import asyncio
from uuid import uuid4

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import make_account, transfer


async def _transaction_count(engine: AsyncEngine) -> int:
    async with engine.connect() as connection:
        return int(
            (await connection.execute(text("SELECT count(*) FROM transactions"))).scalar_one()
        )


async def test_replaying_the_same_key_returns_the_cached_response(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Test 4: one transaction row, byte-identical response, 201 then 200."""
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")
    key = str(uuid4())

    first = await transfer(client, source["id"], destination["id"], 5000, idempotency_key=key)
    second = await transfer(client, source["id"], destination["id"], 5000, idempotency_key=key)

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    assert await _transaction_count(engine) == 1

    async with engine.connect() as connection:
        entries = (await connection.execute(text("SELECT count(*) FROM entries"))).scalar_one()
    assert entries == 2


async def test_same_key_with_a_different_payload_is_rejected(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Test 5: same key, different body is a client bug and must never silently post."""
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")
    key = str(uuid4())

    first = await transfer(client, source["id"], destination["id"], 5000, idempotency_key=key)
    assert first.status_code == 201, first.text

    second = await transfer(client, source["id"], destination["id"], 9999, idempotency_key=key)
    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "idempotency_key_reuse"

    assert await _transaction_count(engine) == 1


async def test_key_order_in_the_request_body_does_not_count_as_a_different_payload(
    client: AsyncClient,
) -> None:
    """The hash is taken over the canonicalised body, not the raw bytes."""
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")
    key = str(uuid4())
    headers = {"Idempotency-Key": key}

    first = await client.post(
        "/transactions",
        headers=headers,
        json={
            "description": "invoice 1042",
            "postings": [
                {"account_id": source["id"], "amount_minor": -5000, "currency": "AUD"},
                {"account_id": destination["id"], "amount_minor": 5000, "currency": "AUD"},
            ],
        },
    )
    second = await client.post(
        "/transactions",
        headers=headers,
        json={
            "postings": [
                {"currency": "AUD", "amount_minor": -5000, "account_id": source["id"]},
                {"currency": "AUD", "amount_minor": 5000, "account_id": destination["id"]},
            ],
            "description": "invoice 1042",
        },
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()


async def test_concurrent_requests_with_the_same_key_create_one_transaction(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Test 6: the unique index decides the winner; the loser replays.

    Both requests attempt the INSERT. Postgres makes the loser wait on the unique index
    until the winner commits, at which point the loser gets a unique violation, re-reads,
    and returns the winner's cached response.
    """
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")
    key = str(uuid4())

    responses = await asyncio.gather(
        *(
            transfer(client, source["id"], destination["id"], 5000, idempotency_key=key)
            for _ in range(8)
        )
    )

    statuses = sorted(response.status_code for response in responses)
    assert statuses.count(201) == 1, [r.text for r in responses]
    assert set(statuses) == {200, 201}

    bodies = [response.json() for response in responses]
    assert all(body == bodies[0] for body in bodies)

    assert await _transaction_count(engine) == 1
    async with engine.connect() as connection:
        entries = (await connection.execute(text("SELECT count(*) FROM entries"))).scalar_one()
    assert entries == 2


async def test_missing_idempotency_key_is_a_400(client: AsyncClient) -> None:
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")

    response = await client.post(
        "/transactions",
        json={
            "postings": [
                {"account_id": source["id"], "amount_minor": -100, "currency": "AUD"},
                {"account_id": destination["id"], "amount_minor": 100, "currency": "AUD"},
            ]
        },
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_required"
