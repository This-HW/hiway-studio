"""Phase-5D schema: audio_generations.params (design report §8.1 / F-13).

Adds a nullable JSON `params` column to `audio_generations` for worker hints
that have no dedicated column: e.g. {"target_duration_sec": 3.2} for the
post-TTS lipsync fit, {"_retry_count": n} for worker retry bookkeeping.
Provider-facing TTS params (text, voice_id, emotion_weights) stay in their
documented columns / the adapter payload.

NOTE: down_revision was 0004 during parallel Phase-5 work; linearized to 0007 at integration.
Phase-5 tracks (5-A/5-B/5-C). If those revisions land first, rebase this
revision onto the new head before merging (alembic heads must be linear).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_phase5d_audio"
down_revision = "0007_phase5c_structure"  # linearized at Phase-5 integration (was branched from 0004)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "audio_generations",
        sa.Column("params", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audio_generations", "params")
