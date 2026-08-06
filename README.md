# ledger-api

A double-entry transaction ledger: money is an append-only log of postings, and a balance
is the sum of that log. Every transaction writes two or more postings that sum to exactly
zero, and PostgreSQL — not the application — is what refuses anything else.

[![CI](https://github.com/YOUR_GITHUB_USERNAME/ledger-api/actions/workflows/ci.yml/badge.svg)](https://github.com/YOUR_GITHUB_USERNAME/ledger-api/actions/workflows/ci.yml)

Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2.0 (async) · Alembic · pytest · Docker

---

## Running it

```bash
docker compose up --build
```

That starts PostgreSQL 16, applies the migrations, and serves the API on
`http://localhost:8000` (interactive docs at `/docs`).

Create two accounts and move $50 between them:

```bash
# A cheque account that may not go overdrawn, and a savings account that may.
ALICE=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' \
  -d '{"name":"alice cheque","currency":"AUD"}' | jq -r .id)

BOB=$(curl -sX POST localhost:8000/accounts \
  -H 'content-type: application/json' \
  -d '{"name":"bob savings","currency":"AUD","allow_negative":true}' | jq -r .id)

# $50.00 from Bob to Alice. Amounts are integer minor units: 5000 == $50.00.
curl -sX POST localhost:8000/transactions \
  -H 'content-type: application/json' \
  -H "Idempotency-Key: $(uuidgen)" \
  -d "{\"description\":\"invoice 1042\",\"postings\":[
        {\"account_id\":\"$BOB\",\"amount_minor\":-5000,\"currency\":\"AUD\"},
        {\"account_id\":\"$ALICE\",\"amount_minor\":5000,\"currency\":\"AUD\"}]}" | jq

curl -s "localhost:8000/accounts/$ALICE/balance" | jq
# {"account_id":"...","currency":"AUD","balance_minor":5000,"as_of":null}
```

### API

| Method | Path | Notes |
|---|---|---|
| `POST` | `/accounts` | create an account |
| `GET` | `/accounts/{id}` | account detail |
| `GET` | `/accounts/{id}/balance` | current balance; optional `?as_of=<rfc3339>` |
| `GET` | `/accounts/{id}/entries` | cursor-paginated log (`?after=<entry_id>&limit=`) |
| `POST` | `/transactions` | requires an `Idempotency-Key` header |
| `GET` | `/transactions/{id}` | transaction and its postings |
| `POST` | `/transactions/{id}/reverse` | post the mirror transaction |
| `GET` | `/healthz` | liveness |

Errors are always the same shape:

```json
{"error": {"code": "insufficient_funds", "message": "account … has 500 and cannot post -600"}}
```

### Tests

The suite runs against a real PostgreSQL, never SQLite — none of the triggers or isolation
behaviour this project is about exists there.

```bash
pip install -e ".[dev]"
pytest -v
```

With `DATABASE_URL` set it uses that database. Without it, `testcontainers` starts a
throwaway PostgreSQL 16 container, so a clean clone with Docker running needs no setup.

---

## Design decisions

**Double-entry rather than a mutable balance column.** A balance column is a running total
with the history thrown away: when it disagrees with what people believe happened, there is
nothing to reconcile against, and every bug that touched it is unrecoverable. Storing
postings instead makes the balance derived, which means it cannot silently drift — if the
sum is wrong, the entries are wrong, and the entries are still there to look at. It also
makes "what was this balance last Tuesday" a query rather than a research project. This is
why every real bank works this way.

**Integer minor units rather than float or decimal.** `amount_minor` is a `BIGINT`: `5000`
means $50.00. Floating point cannot represent `0.10` exactly, so a long enough sequence of
float arithmetic will lose cents, and a ledger that loses cents is not a ledger.
`NUMERIC` would be exact, but it invites a fractional cent into a system where a fractional
cent has no meaning, and it drags rounding-mode decisions into arithmetic that should be
exact addition. Integers make the addition exact by construction and push the only rounding
decision to the edge of the system, where a human can see it.

**A deferred constraint trigger rather than an application check.** The balance rule spans
rows, so a `CHECK` cannot express it, and an ordinary `AFTER INSERT` trigger fires after the
first posting — when the running sum is legitimately non-zero — so nothing would ever
commit. `DEFERRABLE INITIALLY DEFERRED` moves the check to `COMMIT`, once every posting for
the transaction is in place. The reason it lives in the database at all is that an
application check is only as good as the application: a bug in a new code path, a
migration script, or somebody in `psql` all bypass it. The Pydantic validator still rejects
unbalanced postings first, so clients get a clean `422` instead of a database error — it is
the first line of defence, and the trigger is the last.

**Optimistic concurrency rather than `SELECT FOR UPDATE`.** Balances are derived, so
ordinary transfers between unconstrained accounts do not actually race. The race only
appears when a rule reads state before writing — the no-overdraft check — where two
concurrent withdrawals can each read a balance of 500 and each conclude that taking 100 is
fine. PostgreSQL defaults to `READ COMMITTED`, where each statement gets a fresh snapshot,
so a read is not stable across statements and cannot be trusted on its own. Each posting
therefore reads `accounts.version`, validates against the balance, and then
compare-and-swaps the version; a concurrent posting turns that `UPDATE` into `rowcount 0`
and the whole transaction retries against fresh data. Accounts are always touched in sorted
id order, so a transfer A→B and a transfer B→A cannot deadlock against each other.

`SERIALIZABLE` would also be correct, and is a smaller diff, but it converts contention into
serialization failures — so the retry loop has to exist anyway — and it pays that cost on
every transaction rather than only the contended ones.

I would switch to pessimistic locking (`SELECT … FOR UPDATE` in sorted id order) when
contention on a single account is sustained rather than occasional: a hot merchant account
taking constant settlement, for example. Optimistic locking is cheap when writers rarely
collide and quadratic-ish when they always do, because each round of the compare-and-swap
lets exactly one writer through and the rest start over. Pessimistic locking makes each
writer wait once instead of retrying repeatedly. The retry loop is bounded and returns
`409 too_much_contention` rather than spinning, which is the signal that the switch is due.

**Append-only with reversals rather than edits.** `UPDATE` and `DELETE` on `entries` raise,
enforced by triggers. A correction is a new transaction posting the mirror amounts
(`POST /transactions/{id}/reverse`), which means the record of the mistake and the record of
the fix both survive. An edited ledger cannot be audited, because there is no way to tell
an edit from an original.

**`as_of` resolves to a log position, not a timestamp comparison.** `entries.id` is the
authoritative order of the log, so `?as_of=` is resolved once to the highest entry id that
existed at that instant and every subsequent comparison is on ids. Balances are read from
the newest snapshot at or before that position, plus the tail after it, which turns a
balance from a scan of the whole account log into a scan of at most `SNAPSHOT_EVERY`
entries. `test_snapshot_path_and_full_scan_agree` asserts that this and the naive
`SUM(amount_minor)` return the same number at every point in time — that test is what makes
the optimisation safe to trust.

---

## Trade-offs, and what I would do next

- **No multi-currency.** An account has one currency, an entry must match its account's
  currency (trigger-enforced), and a transaction must be single-currency. Real FX needs a
  rate at time of posting and a revaluation account; there is no honest way to bolt it on
  later without those.
- **`entries` is not partitioned.** It is the table that grows forever. Range partitioning
  on `created_at`, or on `id`, is the obvious next step, along with moving cold partitions
  to cheaper storage.
- **Snapshots are written synchronously**, inside the posting transaction, so a posting
  occasionally pays for one. That keeps a snapshot from ever describing a log that does not
  exist, at the cost of a latency spike every `SNAPSHOT_EVERY` entries. A background writer
  would move that cost off the request path.
- **`as_of` is approximate to within in-flight transactions.** `created_at` is the
  transaction's start time while `id` is assigned at insert, so under concurrent writes a
  transaction that started earlier can be assigned a later id. Point-in-time reads are exact
  for a quiesced ledger and can differ by the set of transactions in flight at that instant.
  Making it exact means an explicit commit-order column assigned at commit time.
- **No authentication or authorisation.** Every endpoint is open. This is a ledger core, not
  a service you would expose.
- **Reversals obey the same overdraft rules as ordinary postings**, so reversing a credit an
  account has already spent will be rejected. Real ledgers usually let a reversal through and
  let the account go negative; that is a policy flag I would add rather than a default.

---

## Layout

```
alembic/versions/001_initial_schema.py          tables and indexes
alembic/versions/002_constraints_and_triggers.py the three database-level invariants
src/ledger/models.py                            SQLAlchemy models
src/ledger/schemas.py                           Pydantic request/response, sum-to-zero validator
src/ledger/services/posting.py                  idempotency + optimistic concurrency + append
src/ledger/services/idempotency.py              key claim, request hashing, replay
src/ledger/services/balances.py                 snapshots, as_of, the naive reference sum
src/ledger/main.py                              routes and the error envelope
tests/                                          runs against real PostgreSQL
```

The migrations are the source of truth for the schema, triggers included — a fresh
`alembic upgrade head` gets the invariants, not just the tables.
