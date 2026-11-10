"""Budget reservation + DB idempotency (B5, plan D1/D2/D7).

- llm_calls.status (nullable): 'pending' is committed before the external
  LLM call (budget reservation), then 'succeeded' / 'failed' / 'uncertain'.
  Existing rows stay NULL = completed legacy calls (counted as spent).
- idempotency_records: one accepted job-creating response per
  UNIQUE(method, actual request path, key), with the canonical request hash,
  the stored response and an expiry (IDEMPOTENCY_TTL_SEC).
- budget_holds: carry-over reservation of a PROVIDER_UNCERTAIN attempt that
  a manual retry put back in the queue (period_at = the held attempt's
  budget period).
- generations.reserved_at / audio_generations.reserved_at: budget period
  anchor, set at creation and on each manual retry (backfilled from
  created_at).

Additive; downgrade drops the two tables and the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016_budget_idempotency"
down_revision = "0015_qc_report_seq"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("llm_calls") as batch:
        batch.add_column(sa.Column("status", sa.String(16), nullable=True))
    for table in ("generations", "audio_generations"):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=True))
        op.execute(f"UPDATE {table} SET reserved_at = created_at")
        with op.batch_alter_table(table) as batch:
            batch.alter_column(
                "reserved_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )

    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "method", "path", "key", name="uq_idempotency_records_method_path_key"
        ),
    )
    op.create_index(
        "ix_idempotency_records_expires_at", "idempotency_records", ["expires_at"]
    )

    op.create_table(
        "budget_holds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Uuid(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("amount_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column(
            "reason",
            sa.String(32),
            nullable=False,
            server_default="provider_uncertain",
        ),
        sa.Column(
            "period_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_budget_holds_project_id", "budget_holds", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_budget_holds_project_id", table_name="budget_holds")
    op.drop_table("budget_holds")
    op.drop_index("ix_idempotency_records_expires_at", table_name="idempotency_records")
    op.drop_table("idempotency_records")
    for table in ("audio_generations", "generations"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("reserved_at")
    with op.batch_alter_table("llm_calls") as batch:
        batch.drop_column("status")
