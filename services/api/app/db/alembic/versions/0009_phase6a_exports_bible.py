"""Phase-6A schema: world bible (P2, ADR-0010).

Creates: world_bible_entries (design report Phase 6, world bible).
XML/EDL export adds no tables (deterministic on-demand generation).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_phase6a_exports_bible"
down_revision = "0008_phase5d_audio"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.func.now(), **kw)


def upgrade() -> None:
    # --- world_bible_entries -------------------------------------------------
    op.create_table(
        "world_bible_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("category", sa.String(16), nullable=False, index=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False,
                  server_default=sa.text("''")),
        sa.Column("tags", sa.JSON(), nullable=False,
                  server_default=sa.text("'[]'")),
        _ts("created_at"),
        _ts("updated_at"),
    )


def downgrade() -> None:
    op.drop_table("world_bible_entries")
