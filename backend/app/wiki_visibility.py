"""Visibility of Wiki versions whose recorded sources were deleted."""
from sqlalchemy import select

from . import models


def has_deleted_source(version_id):
    """SQL predicate; works for a version ID or an outer version column."""
    return (
        select(models.WikiEvidence.id)
        .join(models.Document, models.Document.id == models.WikiEvidence.doc_id)
        .where(models.WikiEvidence.page_version_id == version_id,
               models.Document.deleted.is_(True))
        .exists()
    )


def visible_page():
    return ~has_deleted_source(models.WikiPage.current_version_id)
