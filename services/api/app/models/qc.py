"""QC report table (design report §4.17, P0-16).

`checks` is a JSONB array: [{code, status, details, measured, threshold}].
`timeline_content_version` is a Phase-4 addition (not in §4.17): the
timeline's content_version at report creation. A report is "latest" only
when it matches the timeline's current content_version; older reports are
stale and render is refused with 409 QC_FAILED(details.reason='stale')
(design §10.4).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, Uuid, func
from sqlalchemy import JSON
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QCReport(Base):
    __tablename__ = "qc_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Contract enum qc_target: video | timeline. P0 only creates "timeline".
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    # Contract enum qc_result: pass | warn | fail.
    # Transient DB state 'pending' while the q.post worker runs (row is created
    # up front so the task arg stays a single id). The contract enum
    # (contracts/enums.json qc_result: pass|warn|fail) covers completed
    # reports; the API schema exposes overall as a plain string.
    overall: Mapped[str] = mapped_column(String(8), nullable=False)
    checks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # Timeline content_version at creation time (freshness gate). NULL for
    # non-timeline targets.
    timeline_content_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
