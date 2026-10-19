"""Phase-3 schema: video + timeline domains (design report §4.13, §4.15, §4.20).

Creates: videos, export_presets, timelines, timeline_clips (ADR-0007).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_phase3_video_timeline"
down_revision = "0002_phase1_planning_character"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.func.now(), **kw)


def upgrade() -> None:
    # --- videos (depends on shots, generations) ------------------------------
    op.create_table(
        "videos",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shot_id", sa.Uuid(), sa.ForeignKey("shots.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("generation_id", sa.Uuid(), sa.ForeignKey("generations.id"),
                  nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("motion_prompt", sa.Text(), nullable=True),
        sa.Column("duration_sec", sa.Float(), nullable=False),
        sa.Column("fps", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("output_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_videos_shot_id", "videos", ["shot_id"])
    op.create_index("ix_videos_status", "videos", ["status"])

    # --- export_presets (depends on projects) --------------------------------
    op.create_table(
        "export_presets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(32), nullable=True),
        sa.Column("aspect_ratio", sa.String(16), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("fps", sa.Integer(), nullable=False, server_default="24"),
        sa.Column("codec", sa.Text(), nullable=False, server_default="h264"),
        sa.Column("audio_codec", sa.Text(), nullable=False, server_default="aac"),
        sa.Column("burn_subtitles", sa.Boolean(), nullable=False, server_default="false"),
        _ts("created_at"),
    )
    op.create_index("ix_export_presets_project_id", "export_presets", ["project_id"])

    # --- timelines (depends on projects, export_presets) ----------------------
    op.create_table(
        "timelines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False, server_default="기본 타임라인"),
        sa.Column("subtitle_mode", sa.String(16), nullable=False, server_default="single"),
        sa.Column("subtitle_track_ids", sa.JSON(), nullable=True),
        sa.Column("output_preset_id", sa.Uuid(),
                  sa.ForeignKey("export_presets.id"), nullable=True),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
    )
    op.create_index("ix_timelines_project_id", "timelines", ["project_id"])
    op.create_index("ix_timelines_status", "timelines", ["status"])

    # --- timeline_clips (ADR-0007: normalized cut list, D-12) -----------------
    op.create_table(
        "timeline_clips",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("timeline_id", sa.Uuid(),
                  sa.ForeignKey("timelines.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("shot_id", sa.Uuid(),
                  sa.ForeignKey("shots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("video_id", sa.Uuid(),
                  sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("in_sec", sa.Float(), nullable=False, server_default="0"),
        sa.Column("out_sec", sa.Float(), nullable=False),
        _ts("created_at"),
        sa.UniqueConstraint("timeline_id", "position", name="uq_timeline_clips_position"),
    )
    op.create_index("ix_timeline_clips_timeline_id", "timeline_clips", ["timeline_id"])


def downgrade() -> None:
    op.drop_table("timeline_clips")
    op.drop_table("timelines")
    op.drop_table("export_presets")
    op.drop_table("videos")
