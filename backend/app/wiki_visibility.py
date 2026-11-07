"""Visibility of Wiki versions with deleted or unavailable recorded sources."""
from sqlalchemy import or_, select

from . import models


def has_deleted_source(version_id):
    """SQL predicate; works for a version ID or an outer version column."""
    deleted_evidence = (
        select(models.WikiEvidence.id)
        .join(models.Document, models.Document.id == models.WikiEvidence.doc_id)
        .where(models.WikiEvidence.page_version_id == version_id,
               models.Document.deleted.is_(True))
        .correlate_except(models.WikiEvidence, models.Document)
        .exists()
    )
    deleted_generation_source = (
        select(models.WikiGenerationSource.id)
        .outerjoin(models.Document, models.Document.id == models.WikiGenerationSource.doc_ref_id)
        .where(models.WikiGenerationSource.page_version_id == version_id,
               or_(models.Document.id.is_(None), models.Document.deleted.is_(True)))
        .correlate_except(models.WikiGenerationSource, models.Document)
        .exists()
    )
    return deleted_evidence | deleted_generation_source


def visible_page():
    return ~has_deleted_source(models.WikiPage.current_version_id)
