"""Render execution ownership + multishot single fan-out (ADR-0013, plan D8/D9).

timelines gain:
  exec_token (UUID, NULL)                       — issued by request_render's claim
  lease_expires_at (TIMESTAMPTZ, NULL, indexed) — heartbeat-extended lease
  heartbeat_at (TIMESTAMPTZ, NULL)              — NULL = requested, not started
  render_meta (JSON, NULL)                      — published output metadata
    (exec_token, preset, content_version, postflight results, unverified)
multishot_sets gain dispatched_at (reconcile clock while queued).
multishot_frames gain UNIQUE(generation_id) and UNIQUE(shot_id): a child
shot/generation links to one frame only (fan-out happens once, RH2).

Additive: legacy rows keep working. A timeline left 'rendering' from before
this revision has no exec_token; reconcile fails it so the lock never sticks.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_render_execution"
down_revision = "0012_job_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("timelines") as batch:
        batch.add_column(sa.Column("exec_token", sa.Uuid(), nullable=True))
        batch.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(
            sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("render_meta", sa.JSON(), nullable=True))
        batch.create_index("ix_timelines_lease_expires_at", ["lease_expires_at"])

    with op.batch_alter_table("multishot_sets") as batch:
        batch.add_column(
            sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True)
        )

    with op.batch_alter_table("multishot_frames") as batch:
        batch.create_unique_constraint(
            "uq_multishot_frame_generation", ["generation_id"]
        )
        batch.create_unique_constraint("uq_multishot_frame_shot", ["shot_id"])


def downgrade() -> None:
    with op.batch_alter_table("multishot_frames") as batch:
        batch.drop_constraint("uq_multishot_frame_shot", type_="unique")
        batch.drop_constraint("uq_multishot_frame_generation", type_="unique")

    with op.batch_alter_table("multishot_sets") as batch:
        batch.drop_column("dispatched_at")

    with op.batch_alter_table("timelines") as batch:
        batch.drop_index("ix_timelines_lease_expires_at")
        for name in ("render_meta", "heartbeat_at", "lease_expires_at", "exec_token"):
            batch.drop_column(name)
