"""Phase-0 minimal schema: model registry + generations.

Full §4 tables arrive in their own phases; this revision only creates what the
Phase-0 completion path (stub adapter -> task -> file) needs.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_phase0_minimal"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "models",
        sa.Column("model_id", sa.String(128), primary_key=True),
        sa.Column("modality", sa.String(32), nullable=False),
        sa.Column("provider_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("quality_rank", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("params_schema", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("constraints", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("fallback_model_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("pricing_unit", sa.String(32), nullable=False, server_default="per_image"),
        sa.Column("pricing_amount_usd", sa.Numeric(10, 4), nullable=False, server_default="0"),
    )
    op.create_index("ix_models_modality", "models", ["modality"])

    op.create_table(
        "generations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("job_type", sa.String(32), nullable=False, server_default="text_to_image"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("model_id", sa.String(128), nullable=True),
        sa.Column("params", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_generations_project_id", "generations", ["project_id"])
    op.create_index("ix_generations_status", "generations", ["status"])


def downgrade() -> None:
    op.drop_table("generations")
    op.drop_table("models")
