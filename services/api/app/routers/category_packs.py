"""Category pack endpoints (G-17, ADR-0013)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.schemas.style import CategoryPackRead
from app.services import style_kit as style_service

router = APIRouter(tags=["category-packs"])


@router.get("/category-packs", response_model=list[CategoryPackRead])
def list_category_packs(db: Session = Depends(get_db)) -> list[CategoryPackRead]:
    return [CategoryPackRead.model_validate(p) for p in style_service.list_category_packs(db)]
