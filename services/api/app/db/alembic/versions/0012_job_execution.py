"""Job execution ownership columns (ADR-0013, plan D4/D7).

generations + audio_generations gain:
  exec_token (UUID, NULL)            — issued by the queued->running claim
  lease_expires_at (TIMESTAMPTZ, NULL, indexed) — heartbeat-extended lease
  heartbeat_at (TIMESTAMPTZ, NULL)
  submit_state (VARCHAR(16), NOT NULL, default 'none') — none|intent|submitted
  dispatched_at (TIMESTAMPTZ, NULL)  — last broker dispatch (reconcile clock)
audio_generations additionally gains provider_job_id (generations already
has it), retry_count and attempt_count (audio has no attempts table).

Additive and nullable/defaulted: existing rows keep working (submit_state
'none', no lease -> reconcile ignores legacy running rows without a lease).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_job_execution"
down_revision = "0011_phase6c_previz"
branch_labels = None
depends_on = None


def _exec_columns() -> list[sa.Column]:
    return [
        sa.Column("exec_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submit_state", sa.String(16), nullable=False, server_default="none"),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
    ]


def upgrade() -> None:
    with op.batch_alter_table("generations") as batch:
        for col in _exec_columns():
            batch.add_column(col)
        batch.create_index("ix_generations_lease_expires_at", ["lease_expires_at"])

    with op.batch_alter_table("audio_generations") as batch:
        for col in _exec_columns():
            batch.add_column(col)
        batch.add_column(sa.Column("provider_job_id", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.create_index(
            "ix_audio_generations_lease_expires_at", ["lease_expires_at"]
        )


def downgrade() -> None:
    with op.batch_alter_table("audio_generations") as batch:
        batch.drop_index("ix_audio_generations_lease_expires_at")
        for name in (
            "attempt_count",
            "retry_count",
            "provider_job_id",
            "dispatched_at",
            "submit_state",
            "heartbeat_at",
            "lease_expires_at",
            "exec_token",
        ):
            batch.drop_column(name)

    with op.batch_alter_table("generations") as batch:
        batch.drop_index("ix_generations_lease_expires_at")
        for name in (
            "dispatched_at",
            "submit_state",
            "heartbeat_at",
            "lease_expires_at",
            "exec_token",
        ):
            batch.drop_column(name)
