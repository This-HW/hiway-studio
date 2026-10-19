"""Phase-5B schema: generation pipeline (P1-1 multishot, P1-4 LoRA).

Creates: multishot_sets, multishot_frames (design §4.18), lora_models
(design §4.7).

Note: shots.coverage_source_frame_id already exists since
0002_phase1_planning_character; style_kits.extraction_source_ids is
likewise pre-existing. P1-7 style extraction and P1-9 thumbnails add no
new tables (style_extract writes kit fields; thumbnails reuse
generations rows with job_type='thumbnail').
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_phase5b_generation"
down_revision = "0005_phase5a_shot_craft"  # linearized at Phase-5 integration (was branched from 0004)
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.func.now(), **kw)


def upgrade() -> None:
    # --- multishot_sets (design §4.18) ---------------------------------------
    op.create_table(
        "multishot_sets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shot_id", sa.Uuid(),
                  sa.ForeignKey("shots.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("source_frame_generation_id", sa.Uuid(),
                  sa.ForeignKey("generations.id"), nullable=False),
        sa.Column("state_snapshot", sa.JSON(), nullable=False,
                  server_default=sa.text("'{}'")),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default=sa.text("'queued'")),
        _ts("created_at"),
    )
    # --- multishot_frames (design §4.18) -------------------------------------
    op.create_table(
        "multishot_frames",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("multishot_set_id", sa.Uuid(),
                  sa.ForeignKey("multishot_sets.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("coverage_type", sa.String(16), nullable=False),
        sa.Column("generation_id", sa.Uuid(),
                  sa.ForeignKey("generations.id"), nullable=True),
        sa.Column("shot_id", sa.Uuid(),
                  sa.ForeignKey("shots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.Text(), nullable=False,
                  server_default=sa.text("'pending'")),
        sa.Column("approved", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        _ts("created_at"),
        sa.UniqueConstraint("multishot_set_id", "coverage_type",
                            name="uq_multishot_frame_angle"),
    )
    # --- lora_models (design §4.7, P1-4) -------------------------------------
    op.create_table(
        "lora_models",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("character_id", sa.Uuid(),
                  sa.ForeignKey("characters.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("trigger_word", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=True),
        sa.Column("steps", sa.Integer(), nullable=True),
        sa.Column("training_image_count", sa.Integer(), nullable=True),
        sa.Column("training_image_ids", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False,
                  server_default=sa.text("'training'")),
        sa.Column("model_path", sa.Text(), nullable=True),
        sa.Column("base_model", sa.Text(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("generation_id", sa.Uuid(),
                  sa.ForeignKey("generations.id"), nullable=True),
        _ts("created_at"),
    )


def downgrade() -> None:
    op.drop_table("lora_models")
    op.drop_table("multishot_frames")
    op.drop_table("multishot_sets")
