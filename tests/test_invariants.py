"""The three database-level invariants.

The point of these tests is that they bypass the application. Anything the API refuses is
only a convenience; what matters is that PostgreSQL itself refuses to hold a ledger that
does not balance, an entry that has been edited, or an entry in the wrong currency.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from ledger.models import Entry, Transaction
from tests.conftest import make_account, transfer

INSERT_TRANSACTION = text(
    "INSERT INTO transactions (id, idempotency_key, request_hash) "
    "VALUES (:id, :key, 'test') RETURNING id"
)
INSERT_ENTRY = text(
    "INSERT INTO entries (transaction_id, account_id, amount_minor, currency) "
    "VALUES (:transaction_id, :account_id, :amount_minor, :currency)"
)


async def test_unbalanced_postings_are_rejected_at_commit(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Test 1: a transaction whose postings do not sum to zero cannot commit."""
    account = await make_account(client, "solo")

    transaction = Transaction(idempotency_key=str(uuid4()), request_hash="test")
    session.add(transaction)
    await session.flush()
    session.add(
        Entry(
            transaction_id=transaction.id,
            account_id=UUID(account["id"]),
            amount_minor=-5000,
            currency="AUD",
        )
    )

    # The trigger is DEFERRABLE INITIALLY DEFERRED, so the INSERT itself is accepted and
    # the failure surfaces at COMMIT, once every posting has had its chance to arrive.
    with pytest.raises(DBAPIError) as raised:
        await session.commit()
    assert "unbalanced transaction" in str(raised.value)

    await session.rollback()
    remaining = (await session.execute(text("SELECT count(*) FROM entries"))).scalar_one()
    assert remaining == 0


async def test_unbalanced_insert_via_raw_sql_is_rejected(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Test 2: the same thing straight over SQL, proving this is not an application check."""
    account = await make_account(client, "solo")
    transaction_id = uuid4()

    async with engine.connect() as connection:
        await connection.execute(INSERT_TRANSACTION, {"id": transaction_id, "key": str(uuid4())})
        await connection.execute(
            INSERT_ENTRY,
            {
                "transaction_id": transaction_id,
                "account_id": UUID(account["id"]),
                "amount_minor": 1,
                "currency": "AUD",
            },
        )
        with pytest.raises(DBAPIError) as raised:
            await connection.commit()
    assert "unbalanced transaction" in str(raised.value)

    async with engine.connect() as connection:
        count = (await connection.execute(text("SELECT count(*) FROM entries"))).scalar_one()
    assert count == 0


async def test_a_balanced_pair_inserted_by_hand_commits(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """The mirror image of test 2: two postings summing to zero are accepted."""
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")
    transaction_id = uuid4()

    async with engine.connect() as connection:
        await connection.execute(INSERT_TRANSACTION, {"id": transaction_id, "key": str(uuid4())})
        for account_id, amount in ((source["id"], -2500), (destination["id"], 2500)):
            await connection.execute(
                INSERT_ENTRY,
                {
                    "transaction_id": transaction_id,
                    "account_id": UUID(account_id),
                    "amount_minor": amount,
                    "currency": "AUD",
                },
            )
        await connection.commit()

    async with engine.connect() as connection:
        total = (
            await connection.execute(
                text("SELECT sum(amount_minor) FROM entries WHERE transaction_id = :id"),
                {"id": transaction_id},
            )
        ).scalar_one()
    assert total == 0


async def test_entries_cannot_be_updated_or_deleted(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Test 3: the log is append-only. Corrections are reversals, not edits."""
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")
    response = await transfer(client, source["id"], destination["id"], 5000)
    assert response.status_code == 201, response.text
    entry_id = response.json()["postings"][0]["id"]

    async with engine.connect() as connection:
        with pytest.raises(DBAPIError) as raised:
            await connection.execute(
                text("UPDATE entries SET amount_minor = 1 WHERE id = :id"), {"id": entry_id}
            )
        assert "append-only" in str(raised.value)

    async with engine.connect() as connection:
        with pytest.raises(DBAPIError) as raised:
            await connection.execute(text("DELETE FROM entries WHERE id = :id"), {"id": entry_id})
        assert "append-only" in str(raised.value)

    async with engine.connect() as connection:
        amount = (
            await connection.execute(
                text("SELECT amount_minor FROM entries WHERE id = :id"), {"id": entry_id}
            )
        ).scalar_one()
    assert amount == -5000


async def test_entry_currency_must_match_its_account(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Invariant 3.3, enforced BEFORE INSERT so the bad row never lands."""
    account = await make_account(client, "aud account", currency="AUD")
    transaction_id = uuid4()

    async with engine.connect() as connection:
        await connection.execute(INSERT_TRANSACTION, {"id": transaction_id, "key": str(uuid4())})
        with pytest.raises(DBAPIError) as raised:
            await connection.execute(
                INSERT_ENTRY,
                {
                    "transaction_id": transaction_id,
                    "account_id": UUID(account["id"]),
                    "amount_minor": -100,
                    "currency": "USD",
                },
            )
        assert "currency mismatch" in str(raised.value)


async def test_zero_amount_entries_are_rejected(client: AsyncClient, engine: AsyncEngine) -> None:
    """A zero posting is meaningless in a ledger; the CHECK constraint says so."""
    account = await make_account(client, "account")
    transaction_id = uuid4()

    async with engine.connect() as connection:
        await connection.execute(INSERT_TRANSACTION, {"id": transaction_id, "key": str(uuid4())})
        with pytest.raises(DBAPIError):
            await connection.execute(
                INSERT_ENTRY,
                {
                    "transaction_id": transaction_id,
                    "account_id": UUID(account["id"]),
                    "amount_minor": 0,
                    "currency": "AUD",
                },
            )


async def test_api_rejects_unbalanced_postings_before_touching_the_database(
    client: AsyncClient,
) -> None:
    """The Pydantic validator is the first line of defence; the trigger is the last."""
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")

    response = await client.post(
        "/transactions",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "description": "does not balance",
            "postings": [
                {"account_id": source["id"], "amount_minor": -5000, "currency": "AUD"},
                {"account_id": destination["id"], "amount_minor": 4000, "currency": "AUD"},
            ],
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unbalanced_transaction"


async def test_api_rejects_a_posting_in_the_wrong_currency(client: AsyncClient) -> None:
    source = await make_account(client, "source", currency="AUD", allow_negative=True)
    destination = await make_account(client, "destination", currency="AUD")

    response = await client.post(
        "/transactions",
        headers={"Idempotency-Key": str(uuid4())},
        json={
            "postings": [
                {"account_id": source["id"], "amount_minor": -5000, "currency": "USD"},
                {"account_id": destination["id"], "amount_minor": 5000, "currency": "USD"},
            ]
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "currency_mismatch"
