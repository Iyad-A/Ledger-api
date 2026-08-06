"""Domain errors.

Every one of these carries an HTTP status and a stable machine-readable code, so the API
can render a consistent envelope:

    {"error": {"code": "insufficient_funds", "message": "..."}}
"""

from __future__ import annotations


class LedgerError(Exception):
    """Base class for errors that map onto a client-visible HTTP response."""

    status_code: int = 400
    code: str = "ledger_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AccountNotFound(LedgerError):
    status_code = 404
    code = "account_not_found"


class TransactionNotFound(LedgerError):
    status_code = 404
    code = "transaction_not_found"


class CurrencyMismatch(LedgerError):
    status_code = 422
    code = "currency_mismatch"


class InsufficientFunds(LedgerError):
    status_code = 422
    code = "insufficient_funds"


class MissingIdempotencyKey(LedgerError):
    status_code = 400
    code = "idempotency_key_required"


class IdempotencyKeyReuse(LedgerError):
    """Same key, different payload. Always a client bug; never silently post."""

    status_code = 422
    code = "idempotency_key_reuse"


class IdempotencyRequestInFlight(LedgerError):
    """The key is claimed but its response is not yet durable. Safe to retry."""

    status_code = 409
    code = "idempotency_request_in_flight"


class TooMuchContention(LedgerError):
    """Lost the optimistic version check more times than we are willing to retry."""

    status_code = 409
    code = "too_much_contention"
