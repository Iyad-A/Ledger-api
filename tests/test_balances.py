"""Balances: point-in-time reconstruction, the snapshot optimisation, and reversals."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import settings
from ledger.models import BalanceSnapshot
from ledger.services.balances import compute_balance, full_scan_balance
from tests.conftest import balance_of, fund, make_account, transfer


def _at(timestamp: str) -> datetime:
    """The API renders timestamps as ISO strings; the service layer wants a datetime."""
    return datetime.fromisoformat(timestamp)


async def test_as_of_reconstructs_the_balance_at_a_point_in_time(
    client: AsyncClient, session: AsyncSession
) -> None:
    """Test 9: ?as_of= matches a manual sum of the entries up to that instant."""
    account = await make_account(client, "account", allow_negative=True)
    sink = await make_account(client, "sink", allow_negative=True)

    timestamps: list[str] = []
    for _ in range(5):
        response = await transfer(client, account["id"], sink["id"], 100)
        assert response.status_code == 201, response.text
        timestamps.append(response.json()["created_at"])

    for index, timestamp in enumerate(timestamps):
        expected = -100 * (index + 1)
        assert await balance_of(client, account["id"], as_of=timestamp) == expected
        assert (
            await full_scan_balance(session, UUID(account["id"]), as_of=_at(timestamp)) == expected
        )

    assert await balance_of(client, account["id"]) == -500


async def test_a_balance_before_the_first_entry_is_zero(client: AsyncClient) -> None:
    account = await make_account(client, "account", allow_negative=True)
    sink = await make_account(client, "sink", allow_negative=True)

    first = await transfer(client, account["id"], sink["id"], 100)
    created_at = first.json()["created_at"]

    # An instant strictly before the ledger's first entry.
    assert await balance_of(client, account["id"], as_of="2000-01-01T00:00:00Z") == 0
    assert await balance_of(client, account["id"], as_of=created_at) == -100


async def test_snapshot_path_and_full_scan_agree(
    client: AsyncClient, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Test 10: the optimisation is only safe if it is indistinguishable from the naive sum.

    Snapshot every 5 entries so a 20-transfer run produces several, then check the two
    implementations agree at every point in time as well as at the head of the log.
    """
    monkeypatch.setattr(settings, "snapshot_every", 5)

    account = await make_account(client, "account", allow_negative=True)
    sink = await make_account(client, "sink", allow_negative=True)

    timestamps: list[str] = []
    for index in range(20):
        response = await transfer(client, account["id"], sink["id"], 10 * (index + 1))
        assert response.status_code == 201, response.text
        timestamps.append(response.json()["created_at"])

    snapshots = (
        await session.execute(
            select(func.count())
            .select_from(BalanceSnapshot)
            .where(BalanceSnapshot.account_id == UUID(account["id"]))
        )
    ).scalar_one()
    assert snapshots >= 3, "expected the snapshot writer to have fired"

    account_id = UUID(account["id"])
    assert await compute_balance(session, account_id) == await full_scan_balance(
        session, account_id
    )
    for timestamp in timestamps:
        moment = _at(timestamp)
        assert await compute_balance(session, account_id, as_of=moment) == (
            await full_scan_balance(session, account_id, as_of=moment)
        )


async def test_reversal_returns_the_accounts_to_their_previous_balances(
    client: AsyncClient,
) -> None:
    """Test 11: a correction is a new transaction; the original entries are untouched."""
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")
    await fund(client, source["id"], 10_000)

    before_source = await balance_of(client, source["id"])
    before_destination = await balance_of(client, destination["id"])

    posted = await transfer(
        client, source["id"], destination["id"], 2500, description="invoice 1042"
    )
    assert posted.status_code == 201, posted.text
    transaction_id = posted.json()["id"]
    original_postings = posted.json()["postings"]

    assert await balance_of(client, destination["id"]) == before_destination + 2500

    reversal = await client.post(f"/transactions/{transaction_id}/reverse")
    assert reversal.status_code == 201, reversal.text

    assert await balance_of(client, source["id"]) == before_source
    assert await balance_of(client, destination["id"]) == before_destination

    # The original entries are exactly as they were: history is appended to, never edited.
    fetched = await client.get(f"/transactions/{transaction_id}")
    assert fetched.status_code == 200
    assert fetched.json()["postings"] == original_postings

    # And the reversal is a real transaction with mirrored postings.
    reversal_body = reversal.json()
    assert reversal_body["id"] != transaction_id
    assert str(transaction_id) in reversal_body["description"]
    assert sorted(p["amount_minor"] for p in reversal_body["postings"]) == sorted(
        -p["amount_minor"] for p in original_postings
    )


async def test_reversing_twice_replays_the_first_reversal(client: AsyncClient) -> None:
    """The derived idempotency key stops a double-click from double-reversing."""
    source = await make_account(client, "source", allow_negative=True)
    destination = await make_account(client, "destination")

    posted = await transfer(client, source["id"], destination["id"], 700)
    transaction_id = posted.json()["id"]

    first = await client.post(f"/transactions/{transaction_id}/reverse")
    second = await client.post(f"/transactions/{transaction_id}/reverse")

    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()
    assert await balance_of(client, destination["id"]) == 0


async def test_entries_are_cursor_paginated(client: AsyncClient) -> None:
    account = await make_account(client, "account", allow_negative=True)
    sink = await make_account(client, "sink", allow_negative=True)
    for _ in range(7):
        assert (await transfer(client, account["id"], sink["id"], 5)).status_code == 201

    collected: list[int] = []
    cursor: int | None = None
    while True:
        params: dict[str, int] = {"limit": 3}
        if cursor is not None:
            params["after"] = cursor
        page = await client.get(f"/accounts/{account['id']}/entries", params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        collected.extend(item["id"] for item in body["items"])
        cursor = body["next_after"]
        if cursor is None:
            break

    assert len(collected) == 7
    assert collected == sorted(collected)


async def test_unknown_accounts_and_transactions_are_404(client: AsyncClient) -> None:
    missing = uuid4()
    assert (await client.get(f"/accounts/{missing}")).status_code == 404
    assert (await client.get(f"/accounts/{missing}/balance")).status_code == 404
    assert (await client.get(f"/transactions/{missing}")).status_code == 404

    response = await client.get(f"/accounts/{missing}")
    assert response.json()["error"]["code"] == "account_not_found"


async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
