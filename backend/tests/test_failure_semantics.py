"""失败路径的状态语义：状态必须与「检索是否真的可用」一致。

设计前提（见 ingest.py）：新版本解析失败时，旧版本分块**不会被删除**，
所以检索仍然能命中。如果这时把整个知识库标成 failed，
用户会以为库不可用而停止提问，这是错误的状态表达。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app import models
from app.services import worker


@pytest.fixture()
def kb_with_chunk(db_session):
    """一个有可用分块的库，用于验证「失败后仍标为部分可用」。"""
    tag = uuid.uuid4().hex[:8]
    kb = models.KnowledgeBase(name=f"失败语义-{tag}", mode="basic", status="ready")
    db_session.add(kb)
    db_session.commit()
    doc = models.Document(kb_id=kb.id, filename="ok.md", suffix=".md", status="ready")
    db_session.add(doc)
    db_session.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path="/tmp/ok",
                                 status="success")
    db_session.add(ver)
    db_session.commit()
    db_session.add(models.Chunk(
        kb_id=kb.id, doc_id=doc.id, doc_version_id=ver.id, ordinal=0,
        text="这是一段可用于检索的有效内容。", keywords="有效 内容", chunk_hash=f"h{tag}",
    ))
    db_session.commit()
    return kb, doc, ver


def test_parse_failure_keeps_kb_partial_when_old_chunks_exist(db_session, kb_with_chunk, tmp_path):
    kb, doc, ver = kb_with_chunk

    # 造一个损坏的 docx，走真实失败路径
    bad = tmp_path / "broken.docx"
    bad.write_bytes(b"this is not a valid docx at all")

    bad_doc = models.Document(kb_id=kb.id, filename="broken.docx", suffix=".docx",
                              size_bytes=len(bad.read_bytes()), status="indexing")
    db_session.add(bad_doc)
    db_session.commit()
    bad_ver = models.DocumentVersion(
        doc_id=bad_doc.id, version_no=1, storage_path=str(bad), status="queued")
    db_session.add(bad_ver)
    db_session.commit()
    task = models.Task(kind=models.TaskKind.ingest_document.value, status="queued",
                       kb_id=kb.id, doc_id=bad_doc.id, doc_version_id=bad_ver.id,
                       dedupe_key=f"fail-{uuid.uuid4().hex[:6]}")
    db_session.add(task)
    db_session.commit()
    task_id = task.id

    while worker.run_once("test"):
        pass

    db_session.expire_all()
    kb_after = db_session.get(models.KnowledgeBase, kb.id)
    assert kb_after.status == "partial", (
        f"旧分块仍可用时不应标 failed，实际 {kb_after.status}：{kb_after.status_detail}")
    assert "可用" in kb_after.status_detail
    assert "错误" in kb_after.status_detail or "失败" in kb_after.status_detail

    t = db_session.get(models.Task, task_id)
    assert t.status == "failed"
    assert t.error, "失败任务必须有可读原因"

    # 旧内容仍然可检索 —— 这是 partial 而不是 failed 的事实依据
    remaining = db_session.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == kb.id)
    ).scalar()
    assert remaining >= 1


def test_parse_failure_marks_failed_when_no_chunks_left(db_session, tmp_path):
    """库里一个可用分块都没有时，才应该标 failed。"""
    tag = uuid.uuid4().hex[:8]
    kb = models.KnowledgeBase(name=f"空库失败-{tag}", mode="basic", status="ready")
    db_session.add(kb)
    db_session.commit()

    bad = tmp_path / "empty_broken.docx"
    bad.write_bytes(b"broken")
    doc = models.Document(kb_id=kb.id, filename="broken.docx", suffix=".docx", status="indexing")
    db_session.add(doc)
    db_session.commit()
    ver = models.DocumentVersion(doc_id=doc.id, version_no=1, storage_path=str(bad),
                                 status="queued")
    db_session.add(ver)
    db_session.commit()
    db_session.add(models.Task(
        kind=models.TaskKind.ingest_document.value, status="queued", kb_id=kb.id,
        doc_id=doc.id, doc_version_id=ver.id, dedupe_key=f"fail2-{tag}"))
    db_session.commit()

    while worker.run_once("test"):
        pass

    db_session.expire_all()
    kb_after = db_session.get(models.KnowledgeBase, kb.id)
    assert kb_after.status == "failed"
    assert kb_after.status_detail
