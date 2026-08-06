"""Writing a transaction.

This is the only place in the system that appends to the log. Everything interesting about
the design lives here:

  * the idempotency claim, so a retried request cannot post twice
  * optimistic concurrency on accounts.version, so two withdrawals cannot both read the
    same balance and both succeed
  * a fixed lock ordering (sorted account id), so two transfers touching the same pair of
    accounts in opposite directions cannot deadlock
  * a bounded retry loop with jittered backoff, because losing the version check is
    expected under contention rather than exceptional
"""

from __future__ import annotations

import asyncio
import random
from collections import defaultdict
from typing import Any, cast
from uuid import UUID

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.config import settings
from ledger.errors import (
    AccountNotFound,
    CurrencyMismatch,
    IdempotencyKeyReuse,
    IdempotencyRequestInFlight,
    InsufficientFunds,
    TooMuchContention,
)
from ledger.models import Account, Entry, Transaction
from ledger.schemas import EntryOut, PostingIn, TransactionOut
from ledger.services.balances import compute_balance, maybe_snapshot
from ledger.services.idempotency import claim_key, load_by_key


class StaleVersionError(Exception):
    """An account moved under us between our read and our write. Internal; always retried."""


async def create_transaction(
    session: AsyncSession,
    *,
    idempotency_key: str,
    request_hash: str,
    description: str | None,
    postings: list[PostingIn],
    max_retries: int | None = None,
) -> tuple[dict[str, Any], bool]:
    """Append a balanced transaction to the log.

    Returns (response_body, replayed). `replayed` is True when this call did not write
    anything because the idempotency key had already been used for the same payload.
    """
    retries = max_retries if max_retries is not None else settings.max_posting_retries

    for attempt in range(retries):
        transaction = await claim_key(session, idempotency_key, request_hash, description)

        if transaction is None:
            # Somebody else owns this key. Under READ COMMITTED this SELECT gets a fresh
            # snapshot, so the winner's committed row is visible.
            existing = await load_by_key(session, idempotency_key)
            if existing is None:
                # The winner rolled back between our failed INSERT and this read. The key
                # is free again; go round and try to claim it ourselves.
                await session.rollback()
                continue
            if existing.request_hash != request_hash:
                raise IdempotencyKeyReuse(
                    "this Idempotency-Key was already used with a different request body"
                )
            if existing.response_body is None:
                raise IdempotencyRequestInFlight(
                    "a request with this Idempotency-Key is still in flight; retry shortly"
                )
            return dict(existing.response_body), True

        try:
            entries = await _apply_postings(session, transaction, postings)
        except StaleVersionError:
            await session.rollback()
            await _backoff(attempt)
            continue

        body = await _build_response(session, transaction, entries)

        # Cached inside the same transaction as the entries, so a replay can never observe
        # a committed transaction without its response.
        transaction.response_body = body

        # The deferred constraint trigger validates the sum here, at COMMIT.
        await session.commit()
        return body, False

    raise TooMuchContention("too much contention on these accounts, retry")


async def _apply_postings(
    session: AsyncSession, transaction: Transaction, postings: list[PostingIn]
) -> list[Entry]:
    # Fixed lock ordering. If request 1 touches A then B while request 2 touches B then A,
    # they can deadlock; sorting means every request acquires in the same order.
    account_ids = sorted({p.account_id for p in postings})

    rows = (
        (await session.execute(select(Account).where(Account.id.in_(account_ids)))).scalars().all()
    )
    accounts = {a.id: a for a in rows}
    missing = [str(a) for a in account_ids if a not in accounts]
    if missing:
        raise AccountNotFound(f"unknown account(s): {', '.join(missing)}")

    # Read versions BEFORE reading balances. If anything commits a posting to one of these
    # accounts after this point it must also bump the version, so the compare-and-swap
    # below will fail and we will retry against fresh data. Reading the version after the
    # balance would leave a window where a concurrent posting is invisible to both.
    versions = {a.id: a.version for a in rows}

    for posting in postings:
        account = accounts[posting.account_id]
        if account.currency != posting.currency:
            raise CurrencyMismatch(
                f"posting is {posting.currency} but account {account.id} is {account.currency}"
            )

    deltas: dict[UUID, int] = defaultdict(int)
    for posting in postings:
        deltas[posting.account_id] += posting.amount_minor

    for account_id in account_ids:
        account = accounts[account_id]
        if account.allow_negative or deltas[account_id] >= 0:
            continue
        current = await compute_balance(session, account_id)
        if current + deltas[account_id] < 0:
            raise InsufficientFunds(
                f"account {account_id} has {current} and cannot post {deltas[account_id]}"
            )

    # The optimistic check. Postgres blocks a conflicting UPDATE until the other
    # transaction commits, then re-evaluates the WHERE clause against the new row — so a
    # concurrent posting turns this into rowcount 0 rather than a lost update.
    for account_id in account_ids:
        result = cast(
            "CursorResult[Any]",
            await session.execute(
                update(Account)
                .where(Account.id == account_id, Account.version == versions[account_id])
                .values(version=Account.version + 1)
                .execution_options(synchronize_session=False)
            ),
        )
        if result.rowcount == 0:
            raise StaleVersionError(str(account_id))

    entries = [
        Entry(
            transaction_id=transaction.id,
            account_id=posting.account_id,
            amount_minor=posting.amount_minor,
            currency=posting.currency,
        )
        for posting in postings
    ]
    session.add_all(entries)
    await session.flush()

    await maybe_snapshot(session, account_ids)
    return entries


async def _build_response(
    session: AsyncSession, transaction: Transaction, entries: list[Entry]
) -> dict[str, Any]:
    if transaction.created_at is None:
        await session.refresh(transaction, ["created_at"])
    for entry in entries:
        if entry.id is None or entry.created_at is None:
            await session.refresh(entry, ["id", "created_at"])

    return TransactionOut(
        id=transaction.id,
        description=transaction.description,
        created_at=transaction.created_at,
        postings=[EntryOut.model_validate(e) for e in entries],
    ).model_dump(mode="json")


async def _backoff(attempt: int) -> None:
    """Full jitter. Desynchronises the herd when many requests contend on one account."""
    # The exponent is clamped so a large max_posting_retries cannot overflow the float.
    ceiling = min(
        settings.retry_backoff_cap_seconds,
        settings.retry_backoff_base_seconds * (2 ** min(attempt, 16)),
    )
    await asyncio.sleep(random.uniform(0, ceiling))
