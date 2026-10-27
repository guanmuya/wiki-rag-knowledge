"""输出序列化：把 ORM 对象转成前端契约结构，集中一处避免字段漂移。"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models

MODE_LABEL = {"basic": "基础分块", "wiki": "Wiki 增强"}
STATUS_LABEL = {
    "empty": "空库", "indexing": "索引中", "ready": "可用", "partial": "部分可用", "failed": "失败",
}
TASK_LABEL = {
    "ingest_document": "文档入库", "rebuild_wiki": "重建 Wiki",
    "reindex_kb": "重建索引", "embedding_rebuild": "换模型全量重建",
}
TASK_STATUS_LABEL = {
    "queued": "排队中", "running": "处理中", "success": "成功", "partial": "部分失败", "failed": "失败",
}
ROLE_LABEL = {"admin": "管理员", "employee": "普通员工"}
GRANT_LABEL = {"read": "可读", "manage": "可管理"}
WIKI_STATUS_LABEL = {"draft": "草稿", "published": "已发布", "needs_review": "待校对"}
PURPOSE_LABEL = {"llm": "对话模型", "embedding": "向量模型", "vision": "视觉模型"}
PROTOCOL_LABEL = {
    "openai": "OpenAI 兼容（/chat/completions + /embeddings）",
    "openai-responses": "OpenAI Responses（/responses）",
    "anthropic": "Anthropic Messages（/messages）",
}


def iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def user_out(db: Session, u: models.User) -> dict:
    grants = []
    if u.role == models.Role.employee.value:
        rows = db.execute(
            select(models.KbGrant, models.KnowledgeBase)
            .join(models.KnowledgeBase, models.KnowledgeBase.id == models.KbGrant.kb_id)
            .where(models.KbGrant.user_id == u.id)
        ).all()
        grants = [{
            "kb_id": kb.id, "kb_name": kb.name, "level": g.level,
            "level_label": GRANT_LABEL.get(g.level, g.level),
        } for g, kb in rows]
    return {
        "id": u.id, "username": u.username, "display_name": u.display_name,
        "role": u.role, "role_label": ROLE_LABEL.get(u.role, u.role),
        "status": u.status, "status_label": "正常" if u.status == "active" else "已停用",
        "must_change_password": u.must_change_password,
        "created_at": iso(u.created_at), "last_login_at": iso(u.last_login_at),
        "grants": grants, "grant_count": len(grants),
    }


def kb_out(db: Session, kb: models.KnowledgeBase, *, my_level: str | None = None) -> dict:
    doc_count = db.execute(
        select(func.count(models.Document.id)).where(
            models.Document.kb_id == kb.id, models.Document.deleted.is_(False))
    ).scalar() or 0
    chunk_count = db.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == kb.id)
    ).scalar() or 0
    page_count = db.execute(
        select(func.count(models.WikiPage.id)).where(models.WikiPage.kb_id == kb.id)
    ).scalar() or 0
    published = db.execute(
        select(func.count(models.WikiPage.id)).where(
            models.WikiPage.kb_id == kb.id,
            models.WikiPage.status != models.WikiPageStatus.draft.value,
            models.WikiPage.current_version_id.is_not(None),
        )
    ).scalar() or 0
    return {
        "id": kb.id, "name": kb.name, "description": kb.description, "mode": kb.mode,
        "mode_label": MODE_LABEL.get(kb.mode, kb.mode),
        "status": kb.status, "status_label": STATUS_LABEL.get(kb.status, kb.status),
        "status_detail": kb.status_detail,
        "embed_signature": kb.embed_signature, "embed_dim": kb.embed_dim,
        "wiki_ready": bool(published),
        "my_level": my_level, "created_at": iso(kb.created_at),
        "wiki_ready_at": iso(kb.wiki_ready_at),
        "stats": {
            "doc_count": doc_count, "chunk_count": chunk_count,
            "wiki_page_count": page_count, "wiki_published_count": published,
        },
    }


def doc_out(db: Session, d: models.Document) -> dict:
    version = None
    if d.current_version_id:
        v = db.get(models.DocumentVersion, d.current_version_id)
        if v:
            version = {
                "id": v.id, "version_no": v.version_no, "status": v.status,
                "status_label": TASK_STATUS_LABEL.get(v.status, v.status),
                "status_detail": v.status_detail, "chunk_count": v.chunk_count,
                "text_chars": v.text_chars, "created_at": iso(v.created_at),
            }
    return {
        "id": d.id, "kb_id": d.kb_id, "filename": d.filename, "suffix": d.suffix,
        "size_bytes": d.size_bytes, "mime": d.mime,
        "status": d.status, "status_label": STATUS_LABEL.get(d.status, d.status),
        "status_detail": d.status_detail,
        "created_at": iso(d.created_at), "current_version": version,
    }


def task_out(db: Session, t: models.Task) -> dict:
    import json
    kb_name = ""
    doc_name = ""
    if t.kb_id:
        kb = db.get(models.KnowledgeBase, t.kb_id)
        kb_name = kb.name if kb else ""
    if t.doc_id:
        doc = db.get(models.Document, t.doc_id)
        doc_name = doc.filename if doc else ""
    try:
        result = json.loads(t.result or "{}")
    except ValueError:
        result = {}
    return {
        "id": t.id, "kind": t.kind, "kind_label": TASK_LABEL.get(t.kind, t.kind),
        "status": t.status, "status_label": TASK_STATUS_LABEL.get(t.status, t.status),
        "stage": t.stage, "progress": t.progress,
        "kb_id": t.kb_id, "kb_name": kb_name, "doc_id": t.doc_id, "doc_name": doc_name,
        "doc_version_id": t.doc_version_id,
        "error": t.error, "attempts": t.attempts, "result": result,
        # 只加字段：把成本从 result 里提到顶层，前端不必再解包
        # result 结构（result 的形态会随任务类型变化，顶层字段更稳）。
        "usage": result.get("usage") or {},
        "usage_by_phase": result.get("usage_by_phase") or {},
        "created_at": iso(t.created_at), "started_at": iso(t.started_at),
        "finished_at": iso(t.finished_at),
    }


def wiki_page_out(db: Session, p: models.WikiPage) -> dict:
    section_count = 0
    if p.current_version_id:
        section_count = db.execute(
            select(func.count(models.WikiSection.id)).where(
                models.WikiSection.page_version_id == p.current_version_id)
        ).scalar() or 0
    return {
        "id": p.id, "kb_id": p.kb_id, "slug": p.slug, "title": p.title, "summary": p.summary,
        "status": p.status, "status_label": WIKI_STATUS_LABEL.get(p.status, p.status),
        "human_locked": p.human_locked, "parent_slug": p.parent_slug,
        "order_index": p.order_index, "section_count": section_count,
        "current_version_id": p.current_version_id,
        "updated_at": iso(p.updated_at),
    }


def model_out(db: Session, m: models.ModelConfig, *, reveal_key: bool = False) -> dict:
    import json
    try:
        params = json.loads(m.params or "{}")
    except ValueError:
        params = {}
    return {
        "id": m.id, "provider_id": m.provider_id, "provider_name": m.provider.name,
        "purpose": m.purpose, "purpose_label": PURPOSE_LABEL.get(m.purpose, m.purpose),
        "model": m.model, "label": m.label, "params": params,
        "is_default": m.is_default, "enabled": m.enabled,
        "embed_dim": m.embed_dim, "embed_signature": m.embed_signature,
        "last_test_ok": m.last_test_ok, "last_test_at": iso(m.last_test_at),
        "last_test_message": m.last_test_message,
    }


def provider_out(p: models.Provider) -> dict:
    from . import security
    protocol = getattr(p, "protocol", "") or models.DEFAULT_PROTOCOL
    return {
        "id": p.id, "name": p.name, "base_url": p.base_url,
        "api_key_masked": security.mask_secret(p.api_key_enc),
        "has_key": bool(p.api_key_enc), "enabled": p.enabled,
        # protocol 是「该供应商怎么说话」，前端要显示、要能改；
        # protocol_label 给界面直接用，避免前后端各维护一份中文文案。
        "protocol": protocol,
        "protocol_label": PROTOCOL_LABEL.get(protocol, protocol),
        "created_at": iso(p.created_at),
    }


def message_out(m: models.Message) -> dict:
    import json

    def _load(raw, default):
        try:
            return json.loads(raw or "")
        except (ValueError, TypeError):
            return default

    route = _load(m.route, {})
    evidence = _load(m.evidence, [])
    return {
        "id": m.id, "role": m.role, "content": m.content,
        "kb_ids": _load(m.kb_ids, []), "evidence": evidence,
        "route": route, "latency_ms": m.latency_ms, "created_at": iso(m.created_at),
        # 历史消息也要能还原「当时是否证据不足」「消耗了多少模型调用」，
        # 否则刷新页面后统计信息全部丢失，前端只能靠猜。
        "insufficient": bool(route.get("insufficient", False)),
        "usage": route.get("usage", {}),
    }


def conversation_out(c: models.Conversation, *, message_count: int = 0) -> dict:
    import json
    try:
        kb_ids = json.loads(c.kb_ids or "[]")
    except ValueError:
        kb_ids = []
    return {
        "id": c.id, "title": c.title, "kb_ids": kb_ids,
        "message_count": message_count,
        "created_at": iso(c.created_at), "updated_at": iso(c.updated_at),
    }
