"""FastAPI application: routes, error envelope, lifespan."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.exceptions import HTTPException as StarletteHTTPException

from ledger.db import dispose_engine, get_session
from ledger.errors import AccountNotFound, LedgerError, MissingIdempotencyKey, TransactionNotFound
from ledger.models import Account, Entry, Transaction
from ledger.schemas import (
    UNBALANCED_MESSAGE,
    AccountCreate,
    AccountOut,
    BalanceOut,
    EntryOut,
    EntryPage,
    PostingIn,
    TransactionCreate,
    TransactionOut,
)
from ledger.services.balances import compute_balance
from ledger.services.idempotency import canonical_hash
from ledger.services.posting import create_transaction


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    yield
    await dispose_engine()


app = FastAPI(
    title="ledger-api",
    version="0.1.0",
    description="A double-entry transaction ledger with database-enforced invariants.",
    lifespan=lifespan,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKeyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]


# ---------------------------------------------------------------------------
# Error envelope: every failure comes back as {"error": {"code", "message"}}.
# ---------------------------------------------------------------------------


def _error(status_code: int, code: str, message: str, **extra: Any) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"code": code, "message": message, **extra}}
    return JSONResponse(status_code=status_code, content=payload)


@app.exception_handler(LedgerError)
async def _handle_ledger_error(request: Any, exc: LedgerError) -> JSONResponse:
    return _error(exc.status_code, exc.code, exc.message)


@app.exception_handler(RequestValidationError)
async def _handle_validation_error(request: Any, exc: RequestValidationError) -> JSONResponse:
    details = [
        {"loc": [str(part) for part in err.get("loc", ())], "message": str(err.get("msg", ""))}
        for err in exc.errors()
    ]
    code = "validation_error"
    message = "the request body is not valid"
    for detail in details:
        if UNBALANCED_MESSAGE in detail["message"]:
            code = "unbalanced_transaction"
            message = "postings must sum to exactly zero"
            break
    return _error(422, code, message, details=details)


@app.exception_handler(StarletteHTTPException)
async def _handle_http_exception(request: Any, exc: StarletteHTTPException) -> JSONResponse:
    return _error(exc.status_code, "http_error", str(exc.detail))


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@app.post("/accounts", response_model=AccountOut, status_code=201)
async def create_account(payload: AccountCreate, session: SessionDep) -> Account:
    account = Account(
        name=payload.name,
        currency=payload.currency,
        allow_negative=payload.allow_negative,
    )
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return account


async def _load_account(session: AsyncSession, account_id: UUID) -> Account:
    account = (
        await session.execute(select(Account).where(Account.id == account_id))
    ).scalar_one_or_none()
    if account is None:
        raise AccountNotFound(f"unknown account: {account_id}")
    return account


@app.get("/accounts/{account_id}", response_model=AccountOut)
async def get_account(account_id: UUID, session: SessionDep) -> Account:
    return await _load_account(session, account_id)


@app.get("/accounts/{account_id}/balance", response_model=BalanceOut)
async def get_balance(
    account_id: UUID,
    session: SessionDep,
    as_of: Annotated[datetime | None, Query()] = None,
) -> BalanceOut:
    account = await _load_account(session, account_id)
    balance = await compute_balance(session, account_id, as_of=as_of)
    return BalanceOut(
        account_id=account.id,
        currency=account.currency,
        balance_minor=balance,
        as_of=as_of,
    )


@app.get("/accounts/{account_id}/entries", response_model=EntryPage)
async def list_entries(
    account_id: UUID,
    session: SessionDep,
    after: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EntryPage:
    await _load_account(session, account_id)

    query = select(Entry).where(Entry.account_id == account_id)
    if after is not None:
        query = query.where(Entry.id > after)
    query = query.order_by(Entry.id).limit(limit)

    rows = (await session.execute(query)).scalars().all()
    items = [EntryOut.model_validate(row) for row in rows]
    next_after = items[-1].id if len(items) == limit else None
    return EntryPage(items=items, next_after=next_after)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@app.post("/transactions", response_model=TransactionOut, status_code=201)
async def post_transaction(
    payload: TransactionCreate,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    if not idempotency_key:
        raise MissingIdempotencyKey("POST /transactions requires an Idempotency-Key header")

    request_hash = canonical_hash(payload.model_dump(mode="json"))
    body, replayed = await create_transaction(
        session,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        description=payload.description,
        postings=payload.postings,
    )
    return JSONResponse(status_code=200 if replayed else 201, content=body)


async def _load_transaction(session: AsyncSession, transaction_id: UUID) -> Transaction:
    transaction = (
        await session.execute(select(Transaction).where(Transaction.id == transaction_id))
    ).scalar_one_or_none()
    if transaction is None:
        raise TransactionNotFound(f"unknown transaction: {transaction_id}")
    return transaction


async def _load_entries(session: AsyncSession, transaction_id: UUID) -> list[Entry]:
    rows = (
        await session.execute(
            select(Entry).where(Entry.transaction_id == transaction_id).order_by(Entry.id)
        )
    ).scalars()
    return list(rows.all())


@app.get("/transactions/{transaction_id}", response_model=TransactionOut)
async def get_transaction(transaction_id: UUID, session: SessionDep) -> TransactionOut:
    transaction = await _load_transaction(session, transaction_id)
    entries = await _load_entries(session, transaction_id)
    return TransactionOut(
        id=transaction.id,
        description=transaction.description,
        created_at=transaction.created_at,
        postings=[EntryOut.model_validate(entry) for entry in entries],
    )


@app.post("/transactions/{transaction_id}/reverse", response_model=TransactionOut, status_code=201)
async def reverse_transaction(
    transaction_id: UUID,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Post the mirror image of a transaction.

    History is never edited; a correction is itself a transaction. If no Idempotency-Key is
    supplied we derive a deterministic one from the transaction id, so reversing twice
    replays the first reversal instead of double-reversing.
    """
    transaction = await _load_transaction(session, transaction_id)
    original_entries = await _load_entries(session, transaction_id)

    postings = [
        PostingIn(
            account_id=entry.account_id,
            amount_minor=-entry.amount_minor,
            currency=entry.currency,
        )
        for entry in original_entries
    ]
    description = f"reversal of {transaction_id}"
    if transaction.description:
        description = f"{description} ({transaction.description})"

    body, replayed = await create_transaction(
        session,
        idempotency_key=idempotency_key or f"reverse:{transaction_id}",
        request_hash=canonical_hash({"reverse_of": str(transaction_id)}),
        description=description,
        postings=postings,
    )
    return JSONResponse(status_code=200 if replayed else 201, content=body)


# ---------------------------------------------------------------------------
# Operational
# ---------------------------------------------------------------------------


@app.get("/healthz")
async def healthz(session: SessionDep) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}
