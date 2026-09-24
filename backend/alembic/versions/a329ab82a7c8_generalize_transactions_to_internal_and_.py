"""Generalize transactions to internal and token transfers

Revision ID: a329ab82a7c8
Revises: 833647c80257
Create Date: 2026-09-24 15:43:50.113122

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = 'a329ab82a7c8'
down_revision: str | None = '833647c80257'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Hand-written: autogenerate would turn the value_wei rename into drop + add (data loss).
    op.alter_column("transactions", "value_wei", new_column_name="value_raw")
    op.add_column(
        "transactions",
        sa.Column("sub_key", sa.String(length=255), server_default="", nullable=False),
    )
    op.add_column("transactions", sa.Column("token_address", sa.String(length=42)))
    op.add_column("transactions", sa.Column("token_symbol", sa.String()))
    op.add_column("transactions", sa.Column("token_decimals", sa.Integer()))
    op.drop_constraint("transactions_pkey", "transactions", type_="primary")
    op.create_primary_key(
        "transactions_pkey", "transactions", ["source_endpoint", "hash", "sub_key"]
    )
    op.add_column(
        "fetch_log",
        sa.Column("record_count", sa.Integer(), server_default="0", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("fetch_log", "record_count")
    # Only normal transactions fit the old hash-only key.
    op.execute("DELETE FROM transactions WHERE source_endpoint <> 'txlist'")
    op.execute("DELETE FROM fetch_log WHERE endpoint <> 'txlist'")
    op.drop_constraint("transactions_pkey", "transactions", type_="primary")
    op.create_primary_key("transactions_pkey", "transactions", ["hash"])
    op.drop_column("transactions", "token_decimals")
    op.drop_column("transactions", "token_symbol")
    op.drop_column("transactions", "token_address")
    op.drop_column("transactions", "sub_key")
    op.alter_column("transactions", "value_raw", new_column_name="value_wei")
