"""QC report issue order + shot invalidation revision (ADR-0013 B4-r3/r4).

qc_reports gain seq (INTEGER NOT NULL): the per-target issue number 1, 2, ...
"Latest report" (render gate, GET /timelines/{id}/qc-latest) is MAX(seq), not
MAX(created_at): equal timestamps (SQLite 1 s resolution, PG transaction
start time) let an old pass hide a newer fail/pending. New rows get
max(seq) + 1 under the timeline row lock (request_qc).
UNIQUE(target_type, target_id, seq) turns a duplicate into an error instead
of an undefined order.

Backfill: existing rows are numbered per (target_type, target_id) in
(created_at, id) order — the best order available for history. Ties among
legacy rows have NO known issue order (the UUID tie-break is arbitrary), so
B4R4-H1 flags every legacy row whose created_at equals another row's of the
same target as order_ambiguous=true; the render gate refuses an ambiguous
latest report and asks for a fresh QC. Rows created after this migration are
never ambiguous.

shots gain frame_stale_rev (INTEGER NOT NULL DEFAULT 0, B4R4-M1): +1 on every
frame invalidation, so an enqueue can tell a new invalidation of an
already-stale shot from the one it saw at prompt assembly.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015_qc_report_seq"
down_revision = "0014_style_extractions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("qc_reports") as batch:
        batch.add_column(sa.Column("seq", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "order_ambiguous",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
    op.execute(
        sa.text(
            """
            UPDATE qc_reports SET seq = (
                SELECT ranked.rn FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY target_type, target_id
                        ORDER BY created_at, id
                    ) AS rn
                    FROM qc_reports
                ) AS ranked
                WHERE ranked.id = qc_reports.id
            )
            """
        )
    )
    # B4R4-H1: a created_at tie within one target = unknown issue order.
    op.execute(
        sa.text(
            """
            UPDATE qc_reports SET order_ambiguous = :ambiguous
            WHERE EXISTS (
                SELECT 1 FROM qc_reports AS other
                WHERE other.target_type = qc_reports.target_type
                  AND other.target_id = qc_reports.target_id
                  AND other.created_at = qc_reports.created_at
                  AND other.id <> qc_reports.id
            )
            """
        ).bindparams(sa.bindparam("ambiguous", True, type_=sa.Boolean()))
    )
    with op.batch_alter_table("qc_reports") as batch:
        batch.alter_column("seq", existing_type=sa.Integer(), nullable=False)
        batch.create_unique_constraint(
            "uq_qc_reports_target_seq", ["target_type", "target_id", "seq"]
        )
    with op.batch_alter_table("shots") as batch:
        batch.add_column(
            sa.Column(
                "frame_stale_rev", sa.Integer(), nullable=False, server_default="0"
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("shots") as batch:
        batch.drop_column("frame_stale_rev")
    with op.batch_alter_table("qc_reports") as batch:
        batch.drop_constraint("uq_qc_reports_target_seq", type_="unique")
        batch.drop_column("order_ambiguous")
        batch.drop_column("seq")
