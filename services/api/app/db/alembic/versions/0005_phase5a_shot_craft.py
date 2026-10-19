"""Phase-5A schema: shot craft (P1-2 camera presets, P1-3 shot seed, P1-8 prompt snippets).

Creates: camera_presets, prompt_snippets.
Adds to shots: seed (pinned seed), camera_preset_id (FK, SET NULL),
camera_side (left|right|center for the 180-degree check).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_phase5a_shot_craft"
down_revision = "0004_phase4_subtitle_qc_audio"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.func.now(), **kw)


def upgrade() -> None:
    # --- camera_presets ------------------------------------------------------
    op.create_table(
        "camera_presets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("lens", sa.String(64), nullable=True),
        sa.Column("movement", sa.String(24), nullable=True),
        sa.Column("angle", sa.String(24), nullable=True),
        sa.Column("settings", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("created_at"),
    )
    # --- prompt_snippets -----------------------------------------------------
    op.create_table(
        "prompt_snippets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=True, index=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("negative", sa.Text(), nullable=True),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        _ts("created_at"),
        _ts("updated_at"),
    )
    # --- shots: seed + camera craft ------------------------------------------
    # NOTE: the FK-bearing add_column is wrapped in batch mode so the
    # revision also runs on SQLite (plain add_column with a ForeignKey
    # raises NotImplementedError there; production is Postgres).
    op.add_column("shots", sa.Column("seed", sa.Integer(), nullable=True))
    with op.batch_alter_table("shots") as batch:
        batch.add_column(sa.Column("camera_preset_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("camera_side", sa.String(8), nullable=True))
        batch.create_foreign_key(
            "fk_shots_camera_preset_id", "camera_presets",
            ["camera_preset_id"], ["id"], ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("shots") as batch:
        batch.drop_constraint("fk_shots_camera_preset_id", type_="foreignkey")
        batch.drop_column("camera_side")
        batch.drop_column("camera_preset_id")
    op.drop_column("shots", "seed")
    op.drop_table("prompt_snippets")
    op.drop_table("camera_presets")
