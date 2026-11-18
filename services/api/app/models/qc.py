"""QC report table (design report §4.17, P0-16).

`checks` is a JSONB array: [{code, status, details, measured, threshold}].
`timeline_content_version` is a Phase-4 addition (not in §4.17): the
timeline's content_version at report creation. A report is "latest" only
when it matches the timeline's current content_version; older reports are
stale and render is refused with 409 QC_FAILED(details.reason='stale')
(design §10.4).

`seq` (B4R3-H1, ADR-0013 B4-r3) is the per-target issue number: 1, 2, ...
in the order the reports were created. It is THE order of "latest report"
(`created_at` has no tie-break: SQLite CURRENT_TIMESTAMP is 1 s resolution,
PG now() is the transaction start). request_qc assigns it under the
timeline row lock; UNIQUE(target_type, target_id, seq) makes a duplicate an
error instead of an undefined order.

`order_ambiguous` (B4R4-H1) marks legacy reports (before migration 0015)
whose created_at ties with another report of the same target: their issue
order is unknown, so the seq the backfill gave them is arbitrary. The render
gate never accepts an ambiguous latest report (a fresh QC is required).
Reports created by request_qc are never ambiguous.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, Uuid, false, func, select
from sqlalchemy import JSON
from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


def _next_report_seq(context) -> int:
    """Column default of `seq`: max(seq) + 1 of the row's target.

    Runs inside the INSERT's transaction. Request paths hold the timeline row
    lock (request_qc), so the read and the insert are one serialized step.
    Rows inserted in one executemany share `context`; the per-target counter
    cached on it keeps them distinct."""
    params = context.get_current_parameters()
    key = (params["target_type"], params["target_id"])
    cache = context.__dict__.setdefault("_qc_report_seq", {})
    if key not in cache:
        table = QCReport.__table__
        cache[key] = context.connection.execute(
            select(func.coalesce(func.max(table.c.seq), 0)).where(
                table.c.target_type == key[0], table.c.target_id == key[1]
            )
        ).scalar_one()
    cache[key] += 1
    return cache[key]


class QCReport(Base):
    __tablename__ = "qc_reports"
    __table_args__ = (
        UniqueConstraint("target_type", "target_id", "seq", name="uq_qc_reports_target_seq"),
    )

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
    # Per-target issue order (see module docstring). Latest = MAX(seq).
    seq: Mapped[int] = mapped_column(Integer, nullable=False, default=_next_report_seq)
    # Legacy tie of unknown issue order (see module docstring): never authorizes.
    order_ambiguous: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=false()
    )
