"""Real concurrency, not a loop that pretends.

Every request here is issued with asyncio.gather against a client whose dependency
override hands out a fresh session, so each one genuinely runs on its own connection.
Sequential calls in a loop would pass trivially and prove nothing.
"""

from __future__ import annotations

import asyncio

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ledger.config import settings
from tests.conftest import balance_of, fund, make_account, transfer


@pytest.fixture(autouse=True)
def patient_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the optimistic retry loop enough room for a 50-way pile-up on one account.

    Under n-way contention on a single row, each round of the compare-and-swap lets exactly
    one writer through, so the unluckiest request can need on the order of n attempts. The
    production default of 25 is tuned for realistic contention; these tests deliberately
    create unrealistic contention.
    """
    monkeypatch.setattr(settings, "max_posting_retries", 300)
    monkeypatch.setattr(settings, "retry_backoff_cap_seconds", 0.02)


async def test_fifty_concurrent_transfers_all_land_exactly_once(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """Test 7: 50 simultaneous 1c transfers A -> B."""
    source = await make_account(client, "A", allow_negative=True)
    destination = await make_account(client, "B", allow_negative=True)

    responses = await asyncio.gather(
        *(transfer(client, source["id"], destination["id"], 1) for _ in range(50))
    )

    failures = [r.text for r in responses if r.status_code != 201]
    assert not failures, failures

    assert await balance_of(client, source["id"]) == -50
    assert await balance_of(client, destination["id"]) == 50

    async with engine.connect() as connection:
        entries = (await connection.execute(text("SELECT count(*) FROM entries"))).scalar_one()
        transactions = (
            await connection.execute(text("SELECT count(*) FROM transactions"))
        ).scalar_one()
        versions = (
            await connection.execute(text("SELECT sum(version) FROM accounts"))
        ).scalar_one()

    assert entries == 100
    assert transactions == 50
    # Every account touched by a posting bumps exactly once per transaction: 50 x 2.
    assert versions == 100


async def test_concurrent_withdrawals_cannot_overdraw(client: AsyncClient) -> None:
    """Test 8: the balance never goes negative and the losers are told why.

    This is the race the version column exists for. Without it, ten requests each read a
    balance of 500, each conclude a 100 withdrawal is fine, and the account lands at -500.
    """
    account = await make_account(client, "no overdraft", allow_negative=False)
    sink = await make_account(client, "sink", allow_negative=True)
    await fund(client, account["id"], 500)

    responses = await asyncio.gather(
        *(transfer(client, account["id"], sink["id"], 100) for _ in range(10))
    )

    succeeded = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code != 201]

    assert len(succeeded) == 5, [r.text for r in responses]
    assert all(r.status_code in (409, 422) for r in rejected), [r.text for r in rejected]
    assert all(
        r.json()["error"]["code"] in ("insufficient_funds", "too_much_contention") for r in rejected
    )

    assert await balance_of(client, account["id"]) == 0
    # The sink received the five withdrawals that got through.
    assert await balance_of(client, sink["id"]) == 500


async def test_opposing_transfers_do_not_deadlock(client: AsyncClient) -> None:
    """Postings acquire accounts in sorted id order, so A->B and B->A cannot deadlock.

    Without the ordering, one request holds A and wants B while the other holds B and
    wants A, and Postgres kills one of them with a deadlock error.
    """
    left = await make_account(client, "left", allow_negative=True)
    right = await make_account(client, "right", allow_negative=True)

    responses = await asyncio.gather(
        *(
            transfer(client, left["id"], right["id"], 7)
            if index % 2 == 0
            else transfer(client, right["id"], left["id"], 7)
            for index in range(30)
        )
    )

    failures = [r.text for r in responses if r.status_code != 201]
    assert not failures, failures
    assert await balance_of(client, left["id"]) == 0
    assert await balance_of(client, right["id"]) == 0


async def test_the_whole_ledger_always_sums_to_zero(
    client: AsyncClient, engine: AsyncEngine
) -> None:
    """The invariant that makes the system auditable, checked after a concurrent burst."""
    accounts = [
        await make_account(client, f"account {index}", allow_negative=True) for index in range(4)
    ]

    await asyncio.gather(
        *(
            transfer(client, accounts[index % 4]["id"], accounts[(index + 1) % 4]["id"], 13)
            for index in range(40)
        )
    )

    async with engine.connect() as connection:
        total = (
            await connection.execute(text("SELECT coalesce(sum(amount_minor), 0) FROM entries"))
        ).scalar_one()
    assert total == 0
