"""Visibility of Wiki versions with deleted or unavailable recorded sources."""
from sqlalchemy import or_, select

from . import models


def has_deleted_source(version_id):
    """SQL predicate; works for a version ID or an outer version column.

    旧页面只有 ``WikiEvidence`` 时，``doc_id`` 只是一个裸整数：文档行被物理
    删除后，SQLite 可能把这个 ID 分给另一份文档（甚至另一个知识库的文档）。
    若按 ``doc_id`` 直接 join ``Document``，复用后的行看起来仍是「有效来源」，
    已隐藏的页面会重新出现。稳定的身份是证据上的 ``chunk_id`` 外键：分块被
    删除时 SQLite 会把它置空，ID 复用永远无法把它修回来；分块还在时又会把它
    的归属文档钉住，``Document.deleted`` 对软删除仍然可靠。
    """
    deleted_evidence = (
        select(models.WikiEvidence.id)
        .outerjoin(models.Chunk, models.Chunk.id == models.WikiEvidence.chunk_id)
        .outerjoin(models.Document, models.Document.id == models.Chunk.doc_id)
        .where(models.WikiEvidence.page_version_id == version_id,
               models.WikiEvidence.doc_id.isnot(None),
               or_(models.WikiEvidence.chunk_id.is_(None),
                   models.Chunk.id.is_(None),
                   models.Document.deleted.is_(True)))
        .correlate_except(models.WikiEvidence, models.Chunk, models.Document)
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
