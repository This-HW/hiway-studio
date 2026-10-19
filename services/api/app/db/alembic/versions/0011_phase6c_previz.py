"""Phase-6C schema: camera_keyframes (P2 3D camera previz, ADR-0010).

Creates the camera_keyframes table:
  id, shot_id (FK → shots.id ON DELETE CASCADE), t_sec (FLOAT, >= 0),
  position/target (JSON {x, y, z}, meters, Y-up), fov_deg (FLOAT),
  UNIQUE(shot_id, t_sec).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_phase6c_previz"
down_revision = "0009_phase6a_exports_bible"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "camera_keyframes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "shot_id", sa.Uuid(),
            sa.ForeignKey("shots.id", ondelete="CASCADE"),
            nullable=False, index=True,
        ),
        sa.Column("t_sec", sa.Float(), nullable=False),
        sa.Column("position", sa.JSON(), nullable=False),
        sa.Column("target", sa.JSON(), nullable=False),
        sa.Column("fov_deg", sa.Float(), nullable=False,
                  server_default=sa.text("40.0")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("shot_id", "t_sec", name="uq_camera_keyframe_shot_t"),
    )


def downgrade() -> None:
    op.drop_table("camera_keyframes")
