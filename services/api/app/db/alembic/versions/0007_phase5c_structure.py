"""Phase-5C schema: no-op revision (design report §4.3, P1-10).

The `episodes` table already exists since 0002_phase1_planning_character
(model app.models.story.Episode); P1-10 episode endpoints reuse it and add
no columns. This revision keeps the chain monotonic and records the phase.
"""
from __future__ import annotations

revision = "0007_phase5c_structure"
down_revision = "0006_phase5b_generation"  # linearized at Phase-5 integration (was branched from 0004)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
