"""Known race: deleting a source while first Wiki generation is running."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select

from app import models
from app.routers import kbs
from app.services import wiki, worker
from test_wiki_mode_switch import _kb, _switch, store  # noqa: F401


@pytest.mark.xfail(strict=True, reason="首次生成与文档删除尚未协调，已删来源可能留下已发布页面")
def test_initial_generation_does_not_publish_a_deleted_source(store, tmp_path, monkeypatch):
    engine = store.kw["bind"]

    @event.listens_for(engine, "connect")
    def pragmas(conn, _):
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")

    engine.dispose()
    kb_id = _kb(store, chunks=True)
    with store() as db:
        first = db.execute(select(models.Document).where(models.Document.kb_id == kb_id)).scalar_one()
        first_id = first.id
        db.get(models.DocumentVersion, first.current_version_id).storage_path = str(tmp_path / "first.md")
        remaining = models.Document(kb_id=kb_id, filename="remaining.md", status="ready")
        db.add(remaining)
        db.flush()
        version = models.DocumentVersion(doc_id=remaining.id, version_no=1,
                                         storage_path=str(tmp_path / "remaining.md"), status="success")
        db.add(version)
        db.flush()
        db.add(models.Chunk(kb_id=kb_id, doc_id=remaining.id, doc_version_id=version.id,
                            text="Remaining content. " * 10, heading="Remaining",
                            locator="s2", keywords="remaining", chunk_hash="remaining"))
        db.commit()

    _switch(store, kb_id, "wiki")
    original = wiki._map_topics
    deletion = {"rejected": False}

    def delete_while_mapping(db, entries, **kwargs):
        with store() as db2:
            admin = db2.execute(select(models.User)).scalar_one()
            try:
                kbs.delete_document(first_id, db2, admin)
            except HTTPException as exc:
                if exc.status_code != 409:
                    raise
                deletion["rejected"] = True
        return original(db, entries, **kwargs)

    monkeypatch.setattr(wiki, "_map_topics", delete_while_mapping)
    assert worker.run_once("review")
    monkeypatch.setattr(wiki, "_map_topics", original)
    if deletion["rejected"]:
        with store() as db:
            assert not db.get(models.Document, first_id).deleted
        return

    with store() as db:
        first_page = db.execute(select(models.WikiPage).order_by(models.WikiPage.id)).scalars().first()
        first_page_id = first_page.id if first_page else None
    assert worker.run_once("review")
    with store() as db:
        assert db.get(models.Document, first_id).deleted
        stale = db.get(models.WikiPage, first_page_id) if first_page_id else None
        assert stale is None or stale.status == models.WikiPageStatus.draft.value
