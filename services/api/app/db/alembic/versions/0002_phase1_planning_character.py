"""Phase-1 schema: planning + character domains (design report §4.1–§4.11).

Creates: projects, stories, beats, episodes, characters, character_references,
character_wardrobe, assets, style_kits, category_packs, scenes, shots,
shot_references, llm_calls, generation_attempts.
Extends generations to the full §4.11 column set.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_phase1_planning_character"
down_revision = "0001_phase0_minimal"
branch_labels = None
depends_on = None


def _ts(name: str, **kw) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False,
                     server_default=sa.func.now(), **kw)


def upgrade() -> None:
    # Table creation order is dependency-safe for PostgreSQL: a table is only
    # created after every table it references. projects <-> style_kits is a
    # true FK cycle, so style_kits is created without its projects FK first
    # and the FK is added after projects exists.
    # --- category packs (no dependencies) ---------------------------------
    op.create_table(
        "category_packs",
        sa.Column("code", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("pipeline_config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("is_builtin", sa.Boolean(), nullable=False, server_default="false"),
        _ts("created_at"),
    )

    # --- style kits (projects FK added later — cycle) ----------------------
    op.create_table(
        "style_kits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=True),  # FK added below
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_locked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("color", sa.Text(), nullable=True),
        sa.Column("lighting", sa.Text(), nullable=True),
        sa.Column("texture", sa.Text(), nullable=True),
        sa.Column("composition", sa.Text(), nullable=True),
        sa.Column("lens", sa.Text(), nullable=True),
        sa.Column("motion", sa.Text(), nullable=True),
        sa.Column("editing_rhythm", sa.Text(), nullable=True),
        sa.Column("sound", sa.Text(), nullable=True),
        sa.Column("extraction_source_ids", sa.JSON(), nullable=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_style_kits_project", "style_kits", ["project_id"])

    # --- projects ---------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("aspect_ratio", sa.String(8), nullable=False, server_default="16:9"),
        sa.Column("default_style", sa.Text(), nullable=True),
        sa.Column("default_models", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("style_kit_id", sa.Uuid(), sa.ForeignKey("style_kits.id"), nullable=True),
        sa.Column("category_pack_code", sa.String(64),
                  sa.ForeignKey("category_packs.code"), nullable=True),
        sa.Column("monthly_budget_usd", sa.Numeric(10, 2), nullable=True),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="false"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_projects_created_at", "projects", ["created_at"])

    # Break the projects <-> style_kits cycle: the FK goes on after both exist.
    # Batch mode so SQLite (no ALTER ADD CONSTRAINT) and PostgreSQL agree.
    with op.batch_alter_table("style_kits") as bop:
        bop.create_foreign_key(
            "fk_style_kits_project", "projects",
            ["project_id"], ["id"], ondelete="CASCADE",
        )

    # --- stories / beats / episodes ---------------------------------------
    op.create_table(
        "stories",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False, unique=True),
        sa.Column("logline", sa.Text(), nullable=True),
        sa.Column("synopsis", sa.Text(), nullable=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_table(
        "beats",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("story_id", sa.Uuid(),
                  sa.ForeignKey("stories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("act", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
    )
    op.create_index("ix_beats_story_order", "beats", ["story_id", "order_index"])
    op.create_table(
        "episodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("is_template", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("template_source_episode_id", sa.Uuid(),
                  sa.ForeignKey("episodes.id"), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_episodes_project_order", "episodes", ["project_id", "order_index"])

    # --- characters --------------------------------------------------------
    op.create_table(
        "characters",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="supporting"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("appearance_prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=True),
        sa.Column("consistency_method", sa.String(16), nullable=False, server_default="hybrid"),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column("default_strength", sa.Float(), nullable=False, server_default="0.8"),
        sa.Column("voice_id", sa.Text(), nullable=True),
        sa.Column("voice_sample_path", sa.Text(), nullable=True),
        sa.Column("style_notes", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default="false"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_characters_project", "characters", ["project_id"])
    op.create_table(
        "character_references",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("character_id", sa.Uuid(),
                  sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("image_path", sa.Text(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("source", sa.String(16), nullable=False),
        _ts("created_at"),
    )
    op.create_index("ix_character_references_char_role",
                    "character_references", ["character_id", "role"])
    # One primary reference per character (design §4.7). Bare boolean predicate
    # is portable: PostgreSQL boolean and SQLite 0/1 both accept it.
    op.execute(
        "CREATE UNIQUE INDEX uq_character_primary ON character_references (character_id) "
        "WHERE is_primary"
    )
    op.create_table(
        "character_wardrobe",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("character_id", sa.Uuid(),
                  sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prompt_fragment", sa.Text(), nullable=False),
        sa.Column("reference_image_id", sa.Uuid(),
                  sa.ForeignKey("character_references.id"), nullable=True),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default="false"),
        _ts("created_at"),
    )
    op.create_index("ix_character_wardrobe_char", "character_wardrobe", ["character_id"])
    op.execute(
        "CREATE UNIQUE INDEX uq_character_wardrobe_default ON character_wardrobe (character_id) "
        "WHERE is_default"
    )

    # --- assets ------------------------------------------------------------
    op.create_table(
        "assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("image_path", sa.Text(), nullable=False),
        sa.Column("prompt_fragment", sa.Text(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False, server_default="[]"),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_assets_project_kind", "assets", ["project_id", "kind"])

    # --- production (tables only; routers arrive in Phase 2) ----------------
    op.create_table(
        "scenes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("episode_id", sa.Uuid(),
                  sa.ForeignKey("episodes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("beat_id", sa.Uuid(),
                  sa.ForeignKey("beats.id", ondelete="SET NULL"), nullable=True),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("time_of_day", sa.Text(), nullable=True),
        sa.Column("mood", sa.Text(), nullable=True),
        sa.Column("lighting", sa.Text(), nullable=True),
        sa.Column("style_kit_id", sa.Uuid(), sa.ForeignKey("style_kits.id"), nullable=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_scenes_project_order", "scenes", ["project_id", "order_index"])
    op.create_table(
        "shots",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("scene_id", sa.Uuid(),
                  sa.ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("shot_type", sa.String(24), nullable=True),
        sa.Column("camera_angle", sa.String(24), nullable=True),
        sa.Column("movement", sa.String(24), nullable=True),
        sa.Column("duration_sec", sa.Float(), nullable=False, server_default="5.0"),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("dialogue", sa.Text(), nullable=True),
        sa.Column("emotion_weights", sa.JSON(), nullable=True),
        sa.Column("sfx_notes", sa.Text(), nullable=True),
        sa.Column("intended_text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("approved_frame_generation_id", sa.Uuid(),
                  sa.ForeignKey("generations.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("frame_stale", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("continuity_source_shot_id", sa.Uuid(),
                  sa.ForeignKey("shots.id"), nullable=True),
        sa.Column("style_kit_id", sa.Uuid(), sa.ForeignKey("style_kits.id"), nullable=True),
        sa.Column("coverage_source_frame_id", sa.Uuid(), nullable=True),
        _ts("created_at"), _ts("updated_at"),
    )
    op.create_index("ix_shots_scene_order", "shots", ["scene_id", "order_index"])
    op.create_index("ix_shots_status", "shots", ["status"])
    op.create_table(
        "shot_references",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("shot_id", sa.Uuid(),
                  sa.ForeignKey("shots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("character_id", sa.Uuid(),
                  sa.ForeignKey("characters.id", ondelete="CASCADE"), nullable=True),
        sa.Column("asset_id", sa.Uuid(),
                  sa.ForeignKey("assets.id", ondelete="CASCADE"), nullable=True),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("reference_detail_id", sa.Uuid(), nullable=True),
        _ts("created_at"),
        sa.CheckConstraint(
            "(CASE WHEN character_id IS NOT NULL THEN 1 ELSE 0 END + "
            "CASE WHEN asset_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_shot_references_exactly_one_target",
        ),
    )
    op.create_index("ix_shot_references_shot", "shot_references", ["shot_id"])

    # --- llm calls ----------------------------------------------------------
    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(),
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("model_id", sa.String(128),
                  sa.ForeignKey("models.model_id"), nullable=False),
        sa.Column("system_prompt_template", sa.Text(), nullable=False),
        sa.Column("system_prompt_version", sa.String(32), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("output_text", sa.Text(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 6), nullable=True),
        _ts("created_at"),
    )
    op.create_index("ix_llm_calls_project_purpose", "llm_calls", ["project_id", "purpose"])

    # --- generation attempts --------------------------------------------------
    op.create_table(
        "generation_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("generation_id", sa.Uuid(),
                  sa.ForeignKey("generations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("provider_job_id", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
    )
    op.create_index("ix_generation_attempts_gen_no",
                    "generation_attempts", ["generation_id", "attempt_no"])

    # --- extend generations to full §4.11 --------------------------------------
    # Batch mode: SQLite cannot ALTER constraints; batch recreates the table.
    with op.batch_alter_table("generations") as bop:
        bop.add_column(sa.Column("shot_id", sa.Uuid(), nullable=True))
        bop.add_column(sa.Column("parent_generation_id", sa.Uuid(), nullable=True))
        bop.add_column(sa.Column("batch_index", sa.Integer(), nullable=True))
        bop.add_column(sa.Column("is_batch", sa.Boolean(), nullable=False, server_default="false"))
        bop.add_column(sa.Column("progress_pct", sa.Float(), nullable=False, server_default="0"))
        bop.add_column(sa.Column("stage", sa.Text(), nullable=True))
        bop.add_column(sa.Column("model_version", sa.Text(), nullable=True))
        bop.add_column(sa.Column("provider_job_id", sa.Text(), nullable=True))
        bop.add_column(sa.Column("prompt_final", sa.Text(), nullable=True))
        bop.add_column(sa.Column("negative_final", sa.Text(), nullable=True))
        bop.add_column(sa.Column("reference_ids", sa.JSON(), nullable=True))
        bop.add_column(sa.Column("input_paths", sa.JSON(), nullable=True))
        bop.add_column(sa.Column("output_thumbnail_path", sa.Text(), nullable=True))
        bop.add_column(sa.Column("seed", sa.BigInteger(), nullable=True))
        bop.add_column(sa.Column("style_kit_id", sa.Uuid(), nullable=True))
        bop.add_column(sa.Column("style_kit_version", sa.Integer(), nullable=True))
        bop.add_column(sa.Column("estimated_cost_usd", sa.Numeric(10, 6), nullable=True))
        bop.add_column(sa.Column("actual_cost_usd", sa.Numeric(10, 6), nullable=True))
        bop.add_column(sa.Column("error_message", sa.Text(), nullable=True))
        bop.add_column(sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"))
        bop.add_column(sa.Column("auto_fallback", sa.Boolean(), nullable=False, server_default="true"))
        bop.add_column(sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False,
                                server_default=sa.func.now()))
        bop.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        bop.add_column(sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True))
        bop.add_column(sa.Column("canceled_reason", sa.Text(), nullable=True))
        bop.create_foreign_key("fk_generations_project", "projects",
                               ["project_id"], ["id"], ondelete="CASCADE")
        bop.create_foreign_key("fk_generations_shot", "shots",
                               ["shot_id"], ["id"], ondelete="SET NULL")
        bop.create_foreign_key("fk_generations_parent", "generations",
                               ["parent_generation_id"], ["id"], ondelete="CASCADE")
        bop.create_foreign_key("fk_generations_model", "models",
                               ["model_id"], ["model_id"])
        bop.create_foreign_key("fk_generations_style_kit", "style_kits",
                               ["style_kit_id"], ["id"])
        bop.create_index("ix_generations_shot", ["shot_id"])
        bop.create_index("ix_generations_parent", ["parent_generation_id"])


def downgrade() -> None:
    # Reverse dependency order for PostgreSQL (DROP TABLE fails when another
    # table still references the target).
    with op.batch_alter_table("generations") as bop:
        for fk in ("fk_generations_style_kit", "fk_generations_model", "fk_generations_parent",
                   "fk_generations_shot", "fk_generations_project"):
            bop.drop_constraint(fk, type_="foreignkey")
        for ix in ("ix_generations_parent", "ix_generations_shot"):
            bop.drop_index(ix)
        for col in ("shot_id", "parent_generation_id", "batch_index", "is_batch", "progress_pct",
                    "stage", "model_version", "provider_job_id", "prompt_final", "negative_final",
                    "reference_ids", "input_paths", "output_thumbnail_path", "seed", "style_kit_id",
                    "style_kit_version", "estimated_cost_usd", "actual_cost_usd", "error_message",
                    "max_retries", "auto_fallback", "queued_at", "started_at", "finished_at",
                    "canceled_reason"):
            bop.drop_column(col)
    op.drop_table("generation_attempts")
    op.drop_table("llm_calls")
    op.drop_table("shot_references")
    op.drop_table("shots")
    op.drop_table("scenes")
    op.drop_table("assets")
    op.execute("DROP INDEX IF EXISTS uq_character_wardrobe_default")
    op.execute("DROP INDEX IF EXISTS uq_character_primary")
    op.drop_table("character_wardrobe")
    op.drop_table("character_references")
    op.drop_table("characters")
    op.drop_table("episodes")
    op.drop_table("beats")
    op.drop_table("stories")
    # Break the projects <-> style_kits cycle before dropping either side.
    # Batch mode: SQLite cannot ALTER constraints.
    with op.batch_alter_table("style_kits") as bop:
        bop.drop_constraint("fk_style_kits_project", type_="foreignkey")
    op.drop_table("style_kits")
    op.drop_table("projects")
    op.drop_table("category_packs")
