"""Administrator-only recorded usage dashboard endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..db import get_db
from ..deps import require_admin
from ..services import usage

router = APIRouter(tags=["usage"])


@router.get("/usage")
def get_usage(days: int = 30, db: Session = Depends(get_db),
              admin: models.User = Depends(require_admin)):
    """Return recorded counts for the last 7/30 UTC days, or all dates (0)."""
    if days not in (0, 7, 30):
        raise HTTPException(status_code=422, detail="days must be 0, 7, or 30")
    return usage.summarize(db, days)
