"""Pydantic schemas for the QC gate (F-14, design §10 / §4.17)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class QCRequestOut(BaseModel):
    qc_report_id: uuid.UUID


class QCCheckOut(BaseModel):
    code: str
    status: str
    details: str = ""
    measured: Any = None
    threshold: Any = None
    stub: bool = False


class QCReportOut(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    overall: str  # pending | pass | warn | fail
    checks: list[QCCheckOut] = []
    timeline_content_version: int
    created_at: datetime
