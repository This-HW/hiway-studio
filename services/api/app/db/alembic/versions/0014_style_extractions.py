"""Style extraction job rows (ADR-0013 B4a-fix, plan D9).

New table style_extractions: one row per extraction request. Its id is the
q.p1.run_style_extract task argument (ADR-0008); for a project kit it equals
the cost-ledger generation id. Global kits (no project, no ledger) get a row
too, so every extraction has a stable job identity and a terminal guard.

Additive. Ledgers queued before this revision have no extraction row; the
worker treats such a message as a no-op and reconcile fails the stuck ledger
(RECONCILE_P1_STUCK_AFTER_SEC).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014_style_extractions"
down_revision = "0013_render_execution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "style_extractions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "style_kit_id",
            sa.Uuid(),
            sa.ForeignKey("style_kits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("source_ids", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_style_extractions_style_kit_id", "style_extractions", ["style_kit_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_style_extractions_style_kit_id", table_name="style_extractions")
    op.drop_table("style_extractions")
