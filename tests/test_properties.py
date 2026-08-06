"""Property-based test: the ledger sums to zero, whatever you do to it.

Every transaction writes postings summing to zero, so the sum of every balance in the
system is zero at all times. That single invariant is what makes the log auditable, and it
should hold for any sequence of valid transfers rather than for the handful of sequences a
hand-written test happens to try.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

from hypothesis import HealthCheck, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ledger.config import settings
from ledger.models import Account, Entry
from ledger.schemas import PostingIn
from ledger.services.balances import compute_balance, full_scan_balance
from ledger.services.idempotency import canonical_hash
from ledger.services.posting import create_transaction

ACCOUNT_COUNT = 3

transfer_sequences = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=ACCOUNT_COUNT - 1),
        st.integers(min_value=0, max_value=ACCOUNT_COUNT - 1),
        st.integers(min_value=1, max_value=1_000_000),
    ),
    min_size=1,
    max_size=12,
)


@hypothesis_settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(operations=transfer_sequences)
def test_balances_always_sum_to_zero(
    database_url: str, operations: list[tuple[int, int, int]]
) -> None:
    """Test 12: for any random valid transfer sequence, the total is invariant."""
    original_snapshot_every = settings.snapshot_every
    # Small enough that a short sequence still exercises the snapshot writer.
    settings.snapshot_every = 4
    try:
        asyncio.run(_apply_and_check(database_url, operations))
    finally:
        settings.snapshot_every = original_snapshot_every


async def _apply_and_check(database_url: str, operations: list[tuple[int, int, int]]) -> None:
    engine = create_async_engine(database_url, pool_size=5, max_overflow=5)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    try:
        # Fresh accounts per example, so examples cannot interfere with each other and no
        # cleanup between them is needed.
        async with sessions() as session:
            accounts = [
                Account(name=f"property account {index}", currency="AUD", allow_negative=True)
                for index in range(ACCOUNT_COUNT)
            ]
            session.add_all(accounts)
            await session.commit()
            account_ids: list[UUID] = [account.id for account in accounts]

        applied = 0
        for source_index, destination_index, amount in operations:
            if source_index == destination_index:
                continue  # a self-transfer is not a transfer
            postings = [
                PostingIn(
                    account_id=account_ids[source_index],
                    amount_minor=-amount,
                    currency="AUD",
                ),
                PostingIn(
                    account_id=account_ids[destination_index],
                    amount_minor=amount,
                    currency="AUD",
                ),
            ]
            async with sessions() as session:
                await create_transaction(
                    session,
                    idempotency_key=str(uuid4()),
                    request_hash=canonical_hash({"nonce": str(uuid4())}),
                    description=None,
                    postings=postings,
                )
            applied += 1

        async with sessions() as session:
            balances = [await full_scan_balance(session, account_id) for account_id in account_ids]
            entry_count = (
                await session.execute(
                    select(func.count()).select_from(Entry).where(Entry.account_id.in_(account_ids))
                )
            ).scalar_one()

            # The snapshot path must not be able to disagree with the naive sum.
            for account_id, expected in zip(account_ids, balances, strict=True):
                assert await compute_balance(session, account_id) == expected

        assert sum(balances) == 0
        assert entry_count == applied * 2
    finally:
        await engine.dispose()
