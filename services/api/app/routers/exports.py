"""Export-preset routes (P0-18, ADR-0004).

`GET /export-presets` lists the read-only built-ins plus, with `project_id`,
that project's custom presets. Custom presets are created per project; the
built-ins are immutable through the API.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, StudioError
from app.db.session import get_db
from app.schemas.video import ExportPresetCreate, ExportPresetRead, ExportPresetUpdate
from app.services import export_edl as edl_service
from app.services import exports as export_service

router = APIRouter(tags=["export-presets"])


def _read(preset) -> ExportPresetRead:
    data = ExportPresetRead.model_validate(preset)
    data.builtin = preset.project_id is None
    return data


@router.get("/export-presets", response_model=list[ExportPresetRead])
def list_presets(
    project_id: uuid.UUID | None = Query(default=None), db: Session = Depends(get_db)
):
    return [_read(p) for p in export_service.list_presets(db, project_id)]


@router.post("/projects/{project_id}/export-presets", response_model=ExportPresetRead)
def create_preset(
    project_id: uuid.UUID, body: ExportPresetCreate, db: Session = Depends(get_db)
):
    preset = export_service.create_custom_preset(
        db,
        project_id,
        name=body.name,
        aspect_ratio=body.aspect_ratio,
        resolution=body.resolution,
        fps=body.fps,
        codec=body.codec,
        audio_codec=body.audio_codec,
        burn_subtitles=body.burn_subtitles,
    )
    db.commit()
    return _read(preset)


@router.patch("/export-presets/{preset_id}", response_model=ExportPresetRead)
def update_preset(
    preset_id: uuid.UUID, body: ExportPresetUpdate, db: Session = Depends(get_db)
):
    preset = export_service.update_custom_preset(
        db, preset_id, body.model_dump(exclude_unset=True)
    )
    db.commit()
    return _read(preset)


@router.delete("/export-presets/{preset_id}", status_code=204)
def delete_preset(preset_id: uuid.UUID, db: Session = Depends(get_db)):
    export_service.delete_custom_preset(db, preset_id)
    db.commit()


# --- Phase 6: NLE interchange export (P2, ADR-0010) ----------------------------


@router.get("/timelines/{timeline_id}/export")
def export_timeline(
    timeline_id: uuid.UUID,
    format: str = Query(default="xml", pattern="^(xml|edl)$"),
    db: Session = Depends(get_db),
):
    """Export a timeline cut list for external NLEs (Premiere / DaVinci).

    Deterministic, free (no job). `format=xml` -> FCP7 xmeml, `format=edl` ->
    CMX3600 at the timeline fps.
    """
    gathered = edl_service.gather_timeline_media(db, timeline_id)
    if format == "edl":
        body = edl_service.build_cmx3600_edl(gathered).encode("utf-8")
        media_type = "text/plain; charset=utf-8"
        filename = f"timeline-{timeline_id}.edl"
    elif format == "xml":
        body = edl_service.build_fcp7_xml(gathered)
        media_type = "application/xml; charset=utf-8"
        filename = f"timeline-{timeline_id}.xml"
    else:  # unreachable: Query pattern guards it
        raise StudioError(ErrorCode.VALIDATION_ERROR, f"Unknown export format: {format}.")
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
