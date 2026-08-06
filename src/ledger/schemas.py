"""Pydantic request/response models.

The sum-to-zero rule is checked here so a malformed request gets a clean 422 before the
database is touched. The deferred trigger in migration 002 is the last line of defence,
not the first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# Sentinel used by the validation error handler to emit a specific error code rather than
# a generic validation_error.
UNBALANCED_MESSAGE = "postings must sum to zero"

Currency = Annotated[
    str,
    StringConstraints(min_length=3, max_length=3, to_upper=True, pattern=r"^[A-Za-z]{3}$"),
]


class AccountCreate(BaseModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    currency: Currency = "AUD"
    allow_negative: bool = False


class AccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    currency: str
    allow_negative: bool
    version: int
    created_at: datetime


class PostingIn(BaseModel):
    account_id: UUID
    # Signed minor units. Zero is meaningless in a ledger and is rejected by the database
    # CHECK constraint as well.
    amount_minor: int = Field(json_schema_extra={"example": -5000})
    currency: Currency = "AUD"

    @model_validator(mode="after")
    def _amount_is_nonzero(self) -> PostingIn:
        if self.amount_minor == 0:
            raise ValueError("amount_minor must not be zero")
        return self


class TransactionCreate(BaseModel):
    description: str | None = None
    postings: list[PostingIn] = Field(min_length=2)

    @model_validator(mode="after")
    def _balances_and_single_currency(self) -> TransactionCreate:
        total = sum(p.amount_minor for p in self.postings)
        if total != 0:
            raise ValueError(f"{UNBALANCED_MESSAGE}, got {total}")
        if len({p.currency for p in self.postings}) != 1:
            raise ValueError("all postings in a transaction must share one currency")
        return self


class EntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    account_id: UUID
    amount_minor: int
    currency: str
    created_at: datetime


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    description: str | None
    created_at: datetime
    postings: list[EntryOut]


class BalanceOut(BaseModel):
    account_id: UUID
    currency: str
    balance_minor: int
    as_of: datetime | None


class EntryPage(BaseModel):
    items: list[EntryOut]
    # Pass back as ?after= to fetch the next page. None means the log is exhausted.
    next_after: int | None
