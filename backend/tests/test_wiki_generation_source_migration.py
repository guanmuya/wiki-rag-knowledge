"""旧 SQLite 库升级后的 Wiki 生成来源表约束与级联行为。"""
from __future__ import annotations

from typing import NamedTuple

import pytest
from sqlalchemy import Integer, create_engine, delete, event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import db, models


class LegacyIds(NamedTuple):
    kb_id: int
    page_id: int
    version_id: int
    doc_id: int


@pytest.fixture
def legacy_db(tmp_path, monkeypatch):
    """建一个缺少新表、但已有文档和 Wiki 的持久化旧库。"""
    db_path = tmp_path / "legacy.sqlite3"
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    old_tables = [table for table in db.Base.metadata.sorted_tables
                  if table.name != "wiki_generation_sources"]
    db.Base.metadata.create_all(bind=engine, tables=old_tables)
    with Session(engine) as session:
        kb = models.KnowledgeBase(name="升级前的知识库")
        session.add(kb)
        session.flush()
        doc = models.Document(kb_id=kb.id, filename="旧文档.md")
        page = models.WikiPage(kb_id=kb.id, slug="old-page", title="旧页面")
        session.add_all([doc, page])
        session.flush()
        version = models.WikiPageVersion(page_id=page.id, content_md="旧页面正文")
        session.add(version)
        session.flush()
        ids = LegacyIds(kb.id, page.id, version.id, doc.id)
        session.commit()

    # 使用真实 init_db 的路径，但将其 engine 指向隔离的旧库。
    monkeypatch.setattr(db, "engine", engine)
    try:
        yield engine, ids
    finally:
        engine.dispose()


@pytest.fixture
def upgraded_db(legacy_db):
    db.init_db()
    return legacy_db


def test_init_db_creates_missing_table_without_losing_old_data(legacy_db):
    engine, ids = legacy_db
    assert "wiki_generation_sources" not in inspect(engine).get_table_names()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1

    db.init_db()

    inspector = inspect(engine)
    assert "wiki_generation_sources" in inspector.get_table_names()
    columns = {column["name"]: column for column in inspector.get_columns("wiki_generation_sources")}
    assert set(columns) == {"id", "kb_id", "page_version_id", "doc_id", "doc_ref_id"}
    assert inspector.get_pk_constraint("wiki_generation_sources")["constrained_columns"] == ["id"]
    assert all(not columns[name]["nullable"] for name in ("kb_id", "page_version_id", "doc_id"))
    assert columns["doc_ref_id"]["nullable"]
    assert isinstance(columns["doc_id"]["type"], Integer)
    indexes = {tuple(index["column_names"]) for index in inspector.get_indexes("wiki_generation_sources")}
    assert {("kb_id",), ("page_version_id",), ("doc_id",), ("doc_ref_id",)} <= indexes
    foreign_keys = {
        (tuple(key["constrained_columns"]), key["referred_table"],
         tuple(key["referred_columns"]), key["options"].get("ondelete", "").upper())
        for key in inspector.get_foreign_keys("wiki_generation_sources")
    }
    assert foreign_keys == {
        (("kb_id",), "knowledge_bases", ("id",), "CASCADE"),
        (("page_version_id",), "wiki_page_versions", ("id",), "CASCADE"),
        (("doc_ref_id",), "documents", ("id",), "SET NULL"),
    }

    with Session(engine) as session:
        assert session.get(models.KnowledgeBase, ids.kb_id).name == "升级前的知识库"
        assert session.get(models.Document, ids.doc_id).filename == "旧文档.md"
        assert session.get(models.WikiPage, ids.page_id).title == "旧页面"
        assert session.get(models.WikiPageVersion, ids.version_id).content_md == "旧页面正文"
        session.add(models.WikiGenerationSource(
            kb_id=ids.kb_id, page_version_id=ids.version_id,
            doc_id=ids.doc_id, doc_ref_id=ids.doc_id,
        ))
        session.commit()

    # 重复启动不能覆盖来源记录，也不能改变新表的外键和索引。
    db.init_db()
    reloaded_inspector = inspect(engine)
    assert reloaded_inspector.get_foreign_keys("wiki_generation_sources") == inspector.get_foreign_keys(
        "wiki_generation_sources"
    )
    assert reloaded_inspector.get_indexes("wiki_generation_sources") == inspector.get_indexes(
        "wiki_generation_sources"
    )
    with Session(engine) as session:
        source = session.scalars(select(models.WikiGenerationSource)).one()
        assert (source.kb_id, source.page_version_id, source.doc_id, source.doc_ref_id) == (
            ids.kb_id, ids.version_id, ids.doc_id, ids.doc_id,
        )
        assert session.get(models.WikiPageVersion, ids.version_id).content_md == "旧页面正文"


def test_source_pair_is_unique_and_doc_id_is_required(upgraded_db):
    engine, ids = upgraded_db
    with Session(engine) as session:
        other_version = models.WikiPageVersion(page_id=ids.page_id, version_no=2)
        other_doc = models.Document(kb_id=ids.kb_id, filename="另一文档.md")
        session.add_all([other_version, other_doc])
        session.flush()
        session.add_all([
            models.WikiGenerationSource(kb_id=ids.kb_id, page_version_id=ids.version_id,
                                        doc_id=ids.doc_id, doc_ref_id=ids.doc_id),
            models.WikiGenerationSource(kb_id=ids.kb_id, page_version_id=ids.version_id,
                                        doc_id=other_doc.id, doc_ref_id=other_doc.id),
            models.WikiGenerationSource(kb_id=ids.kb_id, page_version_id=other_version.id,
                                        doc_id=ids.doc_id, doc_ref_id=ids.doc_id),
        ])
        session.commit()

        # 即使文档引用已置空，原始来源 ID 仍与版本共同保持唯一。
        session.add(models.WikiGenerationSource(
            kb_id=ids.kb_id, page_version_id=ids.version_id, doc_id=ids.doc_id, doc_ref_id=None,
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

        session.add(models.WikiGenerationSource(
            kb_id=ids.kb_id, page_version_id=ids.version_id, doc_id=None, doc_ref_id=ids.doc_id,
        ))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
        assert len(session.scalars(select(models.WikiGenerationSource)).all()) == 3


def test_document_deletion_and_reused_id_do_not_reconnect_source(upgraded_db):
    engine, ids = upgraded_db
    with Session(engine) as session:
        source = models.WikiGenerationSource(
            kb_id=ids.kb_id, page_version_id=ids.version_id,
            doc_id=ids.doc_id, doc_ref_id=ids.doc_id,
        )
        session.add(source)
        session.commit()
        source_id = source.id

        document = session.get(models.Document, ids.doc_id)
        document.deleted = True
        session.commit()
        source = session.get(models.WikiGenerationSource, source_id)
        assert (source.doc_id, source.doc_ref_id) == (ids.doc_id, ids.doc_id)

        # 清理原文档时只断开引用，保留不可变的历史 ID。
        session.delete(document)
        session.commit()
        session.expire_all()
        source = session.get(models.WikiGenerationSource, source_id)
        assert (source.doc_id, source.doc_ref_id) == (ids.doc_id, None)

        # SQLite 可以重用最高文档 ID；新文档不能自动变成旧页面的来源。
        replacement = models.Document(kb_id=ids.kb_id, filename="新文档.md", deleted=False)
        session.add(replacement)
        session.commit()
        assert replacement.id == ids.doc_id
        session.expire_all()
        source = session.get(models.WikiGenerationSource, source_id)
        assert (source.doc_id, source.doc_ref_id) == (ids.doc_id, None)


def test_source_rejects_missing_referenced_parent(upgraded_db):
    engine, ids = upgraded_db
    with Session(engine) as session:
        for kb_id, page_version_id, doc_ref_id in (
            (999999, ids.version_id, ids.doc_id),
            (ids.kb_id, 999999, ids.doc_id),
            (ids.kb_id, ids.version_id, 999999),
        ):
            session.add(models.WikiGenerationSource(
                kb_id=kb_id, page_version_id=page_version_id, doc_id=ids.doc_id, doc_ref_id=doc_ref_id,
            ))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
        assert session.scalars(select(models.WikiGenerationSource)).all() == []


def test_deleting_page_version_cascades_only_its_sources(upgraded_db):
    engine, ids = upgraded_db
    with Session(engine) as session:
        other_version = models.WikiPageVersion(page_id=ids.page_id, version_no=2)
        other_doc = models.Document(kb_id=ids.kb_id, filename="另一文档.md")
        session.add_all([other_version, other_doc])
        session.flush()
        other_version_id = other_version.id
        session.add_all([
            models.WikiGenerationSource(kb_id=ids.kb_id, page_version_id=ids.version_id,
                                        doc_id=ids.doc_id, doc_ref_id=ids.doc_id),
            models.WikiGenerationSource(kb_id=ids.kb_id, page_version_id=ids.version_id,
                                        doc_id=other_doc.id, doc_ref_id=other_doc.id),
            models.WikiGenerationSource(kb_id=ids.kb_id, page_version_id=other_version_id,
                                        doc_id=ids.doc_id, doc_ref_id=ids.doc_id),
        ])
        session.commit()

        session.execute(delete(models.WikiPageVersion).where(models.WikiPageVersion.id == ids.version_id))
        session.commit()
        sources = session.scalars(select(models.WikiGenerationSource)).all()
        assert [(source.page_version_id, source.doc_id, source.doc_ref_id) for source in sources] == [
            (other_version_id, ids.doc_id, ids.doc_id),
        ]
        assert session.get(models.WikiPage, ids.page_id) is not None


def test_deleting_knowledge_base_cascades_only_its_sources(upgraded_db):
    engine, ids = upgraded_db
    with Session(engine) as session:
        other_kb = models.KnowledgeBase(name="另一知识库")
        session.add(other_kb)
        session.flush()
        other_page = models.WikiPage(kb_id=other_kb.id, slug="other-page", title="另一页面")
        other_doc = models.Document(kb_id=other_kb.id, filename="另一文档.md")
        session.add_all([other_page, other_doc])
        session.flush()
        other_version = models.WikiPageVersion(page_id=other_page.id)
        session.add(other_version)
        session.flush()
        other_kb_id, other_version_id, other_doc_id = other_kb.id, other_version.id, other_doc.id
        session.add_all([
            models.WikiGenerationSource(kb_id=ids.kb_id, page_version_id=ids.version_id,
                                        doc_id=ids.doc_id, doc_ref_id=ids.doc_id),
            models.WikiGenerationSource(kb_id=other_kb_id, page_version_id=other_version_id,
                                        doc_id=other_doc_id, doc_ref_id=other_doc_id),
        ])
        session.commit()

        session.execute(delete(models.KnowledgeBase).where(models.KnowledgeBase.id == ids.kb_id))
        session.commit()
        sources = session.scalars(select(models.WikiGenerationSource)).all()
        assert [(source.kb_id, source.page_version_id, source.doc_id, source.doc_ref_id)
                for source in sources] == [
            (other_kb_id, other_version_id, other_doc_id, other_doc_id),
        ]
