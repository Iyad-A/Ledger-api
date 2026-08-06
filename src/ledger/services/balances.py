"""Balance derivation.

A balance is never stored and mutated; it is the sum of an account's postings. Summing the
whole log gets slow, so we periodically materialise a snapshot at a log position and sum
only the tail after it.

`full_scan_balance` is the naive reference implementation. It exists so the test suite can
assert the snapshot path and the full scan agree — that test is what makes the
optimisation safe to trust.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import settings
from ledger.models import BalanceSnapshot, Entry


async def _horizon_entry_id(session: AsyncSession, as_of: datetime) -> int | None:
    """Resolve a wall-clock instant to a log position.

    entries.id is the authoritative order of the log, so `as_of` is resolved once to the
    highest entry id that existed at that instant, and every subsequent comparison is done
    on ids.
    """
    result = await session.execute(select(func.max(Entry.id)).where(Entry.created_at <= as_of))
    return result.scalar()


async def compute_balance(
    session: AsyncSession, account_id: UUID, *, as_of: datetime | None = None
) -> int:
    """Balance in minor units, using the newest usable snapshot plus the tail after it."""
    horizon: int | None = None
    if as_of is not None:
        horizon = await _horizon_entry_id(session, as_of)
        if horizon is None:
            # No entries existed anywhere in the ledger at that instant.
            return 0

    snapshot_query = select(BalanceSnapshot.up_to_entry_id, BalanceSnapshot.balance_minor).where(
        BalanceSnapshot.account_id == account_id
    )
    if horizon is not None:
        snapshot_query = snapshot_query.where(BalanceSnapshot.up_to_entry_id <= horizon)
    snapshot_query = snapshot_query.order_by(BalanceSnapshot.up_to_entry_id.desc()).limit(1)

    snapshot = (await session.execute(snapshot_query)).first()
    since, base = (int(snapshot[0]), int(snapshot[1])) if snapshot is not None else (0, 0)

    tail_query = select(func.coalesce(func.sum(Entry.amount_minor), 0)).where(
        Entry.account_id == account_id, Entry.id > since
    )
    if as_of is not None:
        tail_query = tail_query.where(Entry.created_at <= as_of)

    tail = (await session.execute(tail_query)).scalar_one()
    return base + int(tail)


async def full_scan_balance(
    session: AsyncSession, account_id: UUID, *, as_of: datetime | None = None
) -> int:
    """Reference implementation: sum every posting, ignore snapshots entirely."""
    query = select(func.coalesce(func.sum(Entry.amount_minor), 0)).where(
        Entry.account_id == account_id
    )
    if as_of is not None:
        query = query.where(Entry.created_at <= as_of)
    return int((await session.execute(query)).scalar_one())


async def maybe_snapshot(session: AsyncSession, account_ids: list[UUID]) -> None:
    """Materialise a snapshot for any account that has just crossed a multiple of N entries.

    Synchronous, inside the posting transaction. If the transaction rolls back the snapshot
    goes with it, so a snapshot can never describe a log that does not exist.
    """
    every = settings.snapshot_every
    if every <= 0:
        return

    for account_id in account_ids:
        count = (
            await session.execute(
                select(func.count()).select_from(Entry).where(Entry.account_id == account_id)
            )
        ).scalar_one()
        if count == 0 or count % every != 0:
            continue

        up_to = (
            await session.execute(select(func.max(Entry.id)).where(Entry.account_id == account_id))
        ).scalar_one()
        balance = await compute_balance(session, account_id)

        await session.execute(
            pg_insert(BalanceSnapshot)
            .values(account_id=account_id, up_to_entry_id=up_to, balance_minor=balance)
            .on_conflict_do_nothing(index_elements=["account_id", "up_to_entry_id"])
        )
