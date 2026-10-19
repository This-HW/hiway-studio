"""Phase-4 schema: subtitles, QC reports, audio (design report §4.14, §4.16, §4.17).

Creates: subtitle_tracks, subtitle_cues, subtitle_styles, qc_reports,
audio_generations, timeline_audio_clips. Adds timelines.content_version
(stale-report gate, design §10.4).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_phase4_subtitle_qc_audio"
down_revision = "0003_phase3_video_timeline"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.func.now(), **kw)


def upgrade() -> None:
    # --- subtitle_tracks -----------------------------------------------------
    op.create_table(
        "subtitle_tracks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("language_code", sa.String(16), nullable=False),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        _ts("created_at"),
        _ts("updated_at"),
    )
    # --- subtitle_cues -------------------------------------------------------
    op.create_table(
        "subtitle_cues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("track_id", sa.Uuid(), sa.ForeignKey("subtitle_tracks.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("word_timings", sa.JSON(), nullable=True),
        sa.Column("style_overrides", sa.JSON(), nullable=True),
        _ts("created_at"),
        sa.CheckConstraint("start_sec > 0", name="ck_cue_start_positive"),
        sa.CheckConstraint("end_sec > start_sec", name="ck_cue_end_after_start"),
    )
    op.create_index("ix_cues_track_start", "subtitle_cues", ["track_id", "start_sec"])
    # --- subtitle_styles -----------------------------------------------------
    op.create_table(
        "subtitle_styles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("track_id", sa.Uuid(), sa.ForeignKey("subtitle_tracks.id", ondelete="CASCADE"),
                  nullable=True, index=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("font_family", sa.Text(), nullable=False, server_default="Noto Sans KR"),
        sa.Column("font_size_px", sa.Integer(), nullable=False, server_default="48"),
        sa.Column("color", sa.Text(), nullable=False, server_default="#FFFFFF"),
        sa.Column("position", sa.String(16), nullable=False, server_default="bottom"),
        sa.Column("background_color", sa.Text(), nullable=True),
        sa.Column("background_opacity", sa.Float(), nullable=False, server_default="0.6"),
        sa.Column("line_break_rule", sa.Text(), nullable=False, server_default="auto"),
        sa.Column("safe_area_margin_pct", sa.Float(), nullable=False, server_default="5.0"),
        _ts("created_at"),
        _ts("updated_at"),
    )
    # --- qc_reports ----------------------------------------------------------
    op.create_table(
        "qc_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False, index=True),
        sa.Column("overall", sa.String(8), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("timeline_content_version", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_qc_target_created", "qc_reports",
                    ["target_type", "target_id", sa.text("created_at DESC")])
    # --- audio_generations ---------------------------------------------------
    op.create_table(
        "audio_generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("shot_id", sa.Uuid(), sa.ForeignKey("shots.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("character_id", sa.Uuid(), sa.ForeignKey("characters.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("voice_id", sa.Text(), nullable=True),
        sa.Column("emotion_weights", sa.JSON(), nullable=True),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="done"),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 6), nullable=True),
        sa.Column("actual_cost_usd", sa.Numeric(10, 6), nullable=True),
        _ts("created_at"),
    )
    # --- timeline_audio_clips ------------------------------------------------
    op.create_table(
        "timeline_audio_clips",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("timeline_id", sa.Uuid(), sa.ForeignKey("timelines.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("audio_id", sa.Uuid(), sa.ForeignKey("audio_generations.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("gain_db", sa.Float(), nullable=False, server_default="0.0"),
        _ts("created_at"),
    )
    op.create_index("ix_audio_clips_timeline_start", "timeline_audio_clips",
                    ["timeline_id", "start_sec"])
    # --- timelines.content_version -------------------------------------------
    op.add_column("timelines",
                  sa.Column("content_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("timelines", "content_version")
    op.drop_index("ix_audio_clips_timeline_start", table_name="timeline_audio_clips")
    op.drop_table("timeline_audio_clips")
    op.drop_table("audio_generations")
    op.drop_index("ix_qc_target_created", table_name="qc_reports")
    op.drop_table("qc_reports")
    op.drop_table("subtitle_styles")
    op.drop_index("ix_cues_track_start", table_name="subtitle_cues")
    op.drop_table("subtitle_cues")
    op.drop_table("subtitle_tracks")
