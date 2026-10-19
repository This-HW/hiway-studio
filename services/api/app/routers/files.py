"""Static file serving (design §5)."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import FileResponse

from app.services import file_serve as file_service

router = APIRouter(tags=["files"])


@router.get("/files/{rel_path:path}")
def serve_file(rel_path: str) -> FileResponse:
    abs_path = file_service.resolve_file(rel_path)
    return FileResponse(abs_path)
