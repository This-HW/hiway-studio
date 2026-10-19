"""QC gate endpoints (F-14, design §10 / §4.17).

POST /timelines/{id}/qc -> 202 {qc_report_id}; the q.post worker runs the
15 checks (13 format + 2 advisory content, P2) and publishes `qc.completed`.
GET /qc-reports/{id} polls the row (overall='pending' while running).
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas import qc as s
from app.services import qc_orchestrator

router = APIRouter(tags=["qc"])


@router.post("/timelines/{timeline_id}/qc", response_model=s.QCRequestOut,
             status_code=202)
def request_qc(timeline_id: uuid.UUID, db: Session = Depends(get_db)):
    return s.QCRequestOut(
        **qc_orchestrator.request_qc(db, timeline_id))


@router.get("/qc-reports/{report_id}", response_model=s.QCReportOut)
def get_report(report_id: uuid.UUID, db: Session = Depends(get_db)):
    r = qc_orchestrator.get_report(db, report_id)
    return s.QCReportOut(
        id=r.id, project_id=r.project_id, target_type=r.target_type,
        target_id=r.target_id, overall=r.overall, checks=r.checks or [],
        timeline_content_version=r.timeline_content_version or 0,
        created_at=r.created_at,
    )


@router.get("/timelines/{timeline_id}/qc-latest", response_model=s.QCReportOut | None)
def latest_report(timeline_id: uuid.UUID, db: Session = Depends(get_db)):
    r = qc_orchestrator.latest_report(db, timeline_id)
    if r is None:
        return None
    return s.QCReportOut(
        id=r.id, project_id=r.project_id, target_type=r.target_type,
        target_id=r.target_id, overall=r.overall, checks=r.checks or [],
        timeline_content_version=r.timeline_content_version or 0,
        created_at=r.created_at,
    )
