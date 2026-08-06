"""SQLAlchemy models.

The migrations own the schema; these mirror it. Money is always a signed integer in minor
units (cents) — never float, never Decimal in the database.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TIMESTAMP


class Base(DeclarativeBase):
    # Fetch server-side defaults (created_at, entries.id) in the INSERT's RETURNING clause
    # rather than issuing a follow-up SELECT.
    __mapper_args__ = {"eager_defaults": True}


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False, server_default="AUD")

    # Optimistic concurrency token. Every posting that touches this account bumps it.
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)

    # When False, a posting that would take this account's balance below zero is rejected.
    allow_negative: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)

    # sha256 of the canonicalised request body. Guards against the same key being reused
    # for a different payload.
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)

    # Cached response, returned verbatim on replay.
    response_body: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class Entry(Base):
    """A single posting. Append-only: the database rejects UPDATE and DELETE."""

    __tablename__ = "entries"
    __table_args__ = (
        CheckConstraint("amount_minor <> 0", name="entries_amount_minor_nonzero"),
        Index("entries_account_id_idx", "account_id", "id"),
        Index("entries_transaction_id_idx", "transaction_id"),
    )

    # Monotonic log position. Snapshots are anchored to it.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("transactions.id"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    # Signed minor units: negative debits the account, positive credits it.
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )


class BalanceSnapshot(Base):
    """A materialised balance for an account as of a given log position."""

    __tablename__ = "balance_snapshots"
    __table_args__ = (PrimaryKeyConstraint("account_id", "up_to_entry_id"),)

    account_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("accounts.id"), nullable=False
    )
    up_to_entry_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
