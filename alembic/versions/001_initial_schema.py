"""initial schema: accounts, transactions, entries, balance_snapshots

Revision ID: 001
Revises:
Create Date: 2026-08-06

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False, server_default=sa.text("'AUD'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("allow_negative", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "entries",
        # BIGSERIAL. This is the log position: monotonic, gapless enough to order by,
        # and what balance snapshots are anchored to.
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        # Signed integer minor units. Negative = debit, positive = credit.
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("amount_minor <> 0", name="entries_amount_minor_nonzero"),
    )
    op.create_index("entries_account_id_idx", "entries", ["account_id", "id"])
    op.create_index("entries_transaction_id_idx", "entries", ["transaction_id"])

    op.create_table(
        "balance_snapshots",
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("accounts.id"),
            nullable=False,
        ),
        sa.Column("up_to_entry_id", sa.BigInteger(), nullable=False),
        sa.Column("balance_minor", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("account_id", "up_to_entry_id"),
    )


def downgrade() -> None:
    op.drop_table("balance_snapshots")
    op.drop_index("entries_transaction_id_idx", table_name="entries")
    op.drop_index("entries_account_id_idx", table_name="entries")
    op.drop_table("entries")
    op.drop_table("transactions")
    op.drop_table("accounts")
