"""database-level invariants: balanced transactions, immutable entries, currency consistency

Revision ID: 002
Revises: 001
Create Date: 2026-08-06

These are deliberately in a migration rather than a stray .sql file: the migrations are
the source of truth for the schema, so a fresh `alembic upgrade head` gets the invariants
too. All three are enforced by Postgres, not by the application, so a buggy service or a
human in psql cannot write a ledger that does not balance.

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Every transaction's postings must sum to exactly zero.
    #
    # A plain CHECK cannot express this because it spans rows. A normal AFTER INSERT
    # trigger cannot either: it would fire after the first posting, when the running sum
    # is legitimately non-zero, and nothing would ever commit. A DEFERRABLE INITIALLY
    # DEFERRED constraint trigger defers the check to COMMIT, once every posting for the
    # transaction is in place.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assert_transaction_balanced() RETURNS trigger AS $$
        DECLARE total BIGINT;
        BEGIN
            SELECT COALESCE(SUM(amount_minor), 0) INTO total
              FROM entries WHERE transaction_id = NEW.transaction_id;
            IF total <> 0 THEN
                RAISE EXCEPTION 'unbalanced transaction %: postings sum to %',
                    NEW.transaction_id, total
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE CONSTRAINT TRIGGER entries_must_balance
            AFTER INSERT ON entries
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION assert_transaction_balanced();
        """
    )

    # ------------------------------------------------------------------
    # 2. Entries are append-only. Corrections are made by posting a reversing
    #    transaction (POST /transactions/{id}/reverse), never by editing history.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION reject_entry_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'entries are append-only: % is not permitted', TG_OP
                USING ERRCODE = 'restrict_violation';
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER entries_no_update BEFORE UPDATE ON entries
            FOR EACH ROW EXECUTE FUNCTION reject_entry_mutation();
        """
    )
    op.execute(
        """
        CREATE TRIGGER entries_no_delete BEFORE DELETE ON entries
            FOR EACH ROW EXECUTE FUNCTION reject_entry_mutation();
        """
    )

    # ------------------------------------------------------------------
    # 3. An entry's currency must match the currency of the account it posts to.
    #    Checked BEFORE INSERT so the bad row never lands.
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE OR REPLACE FUNCTION assert_entry_currency_matches_account() RETURNS trigger AS $$
        DECLARE account_currency CHAR(3);
        BEGIN
            SELECT currency INTO account_currency FROM accounts WHERE id = NEW.account_id;
            IF account_currency IS NULL THEN
                RAISE EXCEPTION 'entry references unknown account %', NEW.account_id
                    USING ERRCODE = 'foreign_key_violation';
            END IF;
            IF account_currency <> NEW.currency THEN
                RAISE EXCEPTION 'currency mismatch: entry is % but account % is %',
                    NEW.currency, NEW.account_id, account_currency
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER entries_currency_matches_account BEFORE INSERT ON entries
            FOR EACH ROW EXECUTE FUNCTION assert_entry_currency_matches_account();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS entries_currency_matches_account ON entries;")
    op.execute("DROP FUNCTION IF EXISTS assert_entry_currency_matches_account();")
    op.execute("DROP TRIGGER IF EXISTS entries_no_delete ON entries;")
    op.execute("DROP TRIGGER IF EXISTS entries_no_update ON entries;")
    op.execute("DROP FUNCTION IF EXISTS reject_entry_mutation();")
    op.execute("DROP TRIGGER IF EXISTS entries_must_balance ON entries;")
    op.execute("DROP FUNCTION IF EXISTS assert_transaction_balanced();")
