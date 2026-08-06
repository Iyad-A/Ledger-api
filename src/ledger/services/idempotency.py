"""Idempotency-Key handling, in the shape Stripe uses.

The contract:

  * Same key, same payload  -> the first response is replayed, 200.
  * Same key, different payload -> 422. Never silently post a second time.
  * Two concurrent requests with the same key -> exactly one transaction exists.

The race is handled by the unique index on transactions.idempotency_key, not by the
application. The loser's INSERT blocks on the index until the winner commits or rolls
back, then either raises a unique violation (winner committed; replay its response) or
succeeds (winner rolled back; the loser becomes the winner).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ledger.models import Transaction


def canonical_hash(body: dict[str, Any]) -> str:
    """sha256 of the request body with keys sorted and whitespace normalised.

    Hashing the parsed body rather than the raw bytes means key order and formatting
    differences between two otherwise identical requests do not read as a payload change.
    """
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def claim_key(
    session: AsyncSession,
    idempotency_key: str,
    request_hash: str,
    description: str | None,
) -> Transaction | None:
    """Try to become the owner of this key.

    Returns the new (unflushed to commit, but INSERTed) transaction on success, or None if
    the key is already taken. The INSERT happens inside a SAVEPOINT so that a unique
    violation does not poison the surrounding transaction.
    """
    transaction = Transaction(
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        description=description,
    )
    try:
        async with session.begin_nested():
            session.add(transaction)
            await session.flush()
    except IntegrityError:
        return None
    return transaction


async def load_by_key(session: AsyncSession, idempotency_key: str) -> Transaction | None:
    result = await session.execute(
        select(Transaction).where(Transaction.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()
