"""Wiki 浏览与人工校对。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from .. import models, schemas, serializers, wiki_visibility
from ..db import get_db
from ..deps import audit, get_current_user, grant_level
from ..services import chunking, cost, llm, wiki

router = APIRouter(tags=["wiki"])


def _require(db: Session, user: models.User, kb_id: int, level: str = "read") -> models.KnowledgeBase:
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    lv = grant_level(db, user, kb_id)
    if lv is None:
        raise HTTPException(status_code=403, detail="没有该知识库的访问权限")
    if level == "manage" and lv != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
    return kb


def _page_for_user(db: Session, user: models.User, kb_id: int, slug: str) -> models.WikiPage:
    stmt = select(models.WikiPage).where(models.WikiPage.kb_id == kb_id, models.WikiPage.slug == slug)
    if user.role != models.Role.admin.value:
        stmt = stmt.where(wiki_visibility.visible_page())
    page = db.execute(stmt).scalar_one_or_none()
    if not page:
        raise HTTPException(status_code=404, detail="Wiki 页面不存在")
    return page


@router.get("/kbs/{kb_id}/wiki/tree")
def wiki_tree(kb_id: int, db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    kb = _require(db, user, kb_id)
    stmt = select(models.WikiPage).where(models.WikiPage.kb_id == kb_id)
    if user.role != models.Role.admin.value:
        stmt = stmt.where(wiki_visibility.visible_page())
    pages = db.execute(stmt.order_by(models.WikiPage.order_index, models.WikiPage.id)).scalars().all()
    items = [serializers.wiki_page_out(db, p) for p in pages]
    return {
        "items": items, "total": len(items),
        "mode": kb.mode, "mode_label": serializers.MODE_LABEL.get(kb.mode, kb.mode),
        "wiki_ready": serializers.wiki_ready(db, kb_id),
        "status_detail": kb.status_detail,
        "empty_reason": "" if pages else (
            "暂无可查看的 Wiki 页面。文档入库会触发生成；如页面暂不可用，请查看任务或联系管理员。"
            if kb.mode == models.KbMode.wiki.value else
            "暂无可查看的 Wiki 页面。当前为基础分块模式。"
        ),
    }


@router.get("/kbs/{kb_id}/wiki/pages/{slug}")
def wiki_page(kb_id: int, slug: str, db: Session = Depends(get_db),
              user: models.User = Depends(get_current_user)):
    _require(db, user, kb_id)
    page = _page_for_user(db, user, kb_id, slug)
    version = db.get(models.WikiPageVersion, page.current_version_id) if page.current_version_id else None
    data = serializers.wiki_page_out(db, page)
    data["version"] = {
        "id": version.id, "version_no": version.version_no, "content_md": version.content_md,
        "summary": version.summary, "origin": version.origin, "note": version.note,
        "created_at": serializers.iso(version.created_at),
    } if version else None
    data["evidence"] = wiki_evidence(db, version.id) if version else []
    if user.role == models.Role.admin.value:
        deleted_sources = db.execute(
            select(models.WikiGenerationSource.doc_id, models.Document.filename)
            .outerjoin(models.Document, models.Document.id == models.WikiGenerationSource.doc_ref_id)
            .where(models.WikiGenerationSource.page_version_id == version.id,
                   or_(models.Document.id.is_(None), models.Document.deleted.is_(True)))
            .distinct()
            .order_by(models.WikiGenerationSource.doc_id)
        ).all() if version else []
        data["deleted_generation_sources"] = [
            {"id": doc_id, "name": filename if filename is not None else f"已不可用的文档 #{doc_id}"}
            for doc_id, filename in deleted_sources
        ]
    data["related"] = wiki.related_pages(db, kb_id, page, include_hidden=user.role == models.Role.admin.value)
    return data


def wiki_evidence(db: Session, page_version_id: int) -> list[dict]:
    rows = db.execute(
        select(models.WikiEvidence).where(models.WikiEvidence.page_version_id == page_version_id)
        .order_by(models.WikiEvidence.id)
    ).scalars().all()
    if not rows:
        return []
    # ``doc_id`` 只是裸整数：来源文档被物理删除后，SQLite 可能把这个 ID 分给
    # 另一份文档（甚至另一个知识库的文档）。只有仍指向活分块、且分块记录的
    # 文档 ID / 知识库 ID 与证据一致时，才敢拿它去查文件名，否则会误显示
    # 别人的文件名。已删除来源的名称由 deleted_generation_sources 单独展示。
    chunk_ids = {r.chunk_id for r in rows if r.chunk_id is not None}
    live_chunks = {c.id: (c.doc_id, c.kb_id) for c in db.execute(
        select(models.Chunk).where(models.Chunk.id.in_(chunk_ids))).scalars()} if chunk_ids else {}
    stable_doc_ids = {
        r.doc_id for r in rows
        if r.doc_id is not None and live_chunks.get(r.chunk_id) == (r.doc_id, r.kb_id)
    }
    docs = {d.id: d for d in db.execute(
        select(models.Document).where(models.Document.id.in_(stable_doc_ids))).scalars()} if stable_doc_ids else {}
    ver_ids = {r.doc_version_id for r in rows if r.doc_version_id}
    vers = {v.id: v for v in db.execute(
        select(models.DocumentVersion).where(models.DocumentVersion.id.in_(ver_ids))).scalars()} if ver_ids else {}
    out = []
    for r in rows:
        doc = docs.get(r.doc_id)
        ver = vers.get(r.doc_version_id)
        out.append({
            "chunk_id": r.chunk_id, "doc_id": r.doc_id,
            "doc_name": doc.filename if doc else "", "doc_version_id": r.doc_version_id,
            "version_no": ver.version_no if ver else None, "locator": r.locator, "quote": r.quote,
        })
    return out


@router.put("/kbs/{kb_id}/wiki/pages/{slug}")
def update_wiki_page(kb_id: int, slug: str, body: schemas.WikiPageUpdateIn,
                     db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    kb = _require(db, user, kb_id, level="manage")
    page = _page_for_user(db, user, kb_id, slug)
    content = body.content_md.strip()
    if len(content) < 10:
        raise HTTPException(status_code=400, detail="正文内容过短")

    previous_version_id = page.current_version_id
    # 版本并发：旧草稿不能盖掉已经用有效来源重新生成的版本。
    if body.expected_version_id is not None and body.expected_version_id != previous_version_id:
        raise HTTPException(status_code=409, detail="版本冲突：页面已更新，请刷新后重新编辑")
    if body.expected_version_id is None:
        # 旧调用方不带版本令牌：若提交的正文与某个「来源已失效」的历史版本
        # 逐字相同，说明它是一份缓存的旧草稿，拒绝重新公开已删除内容。
        stale = db.execute(
            select(models.WikiPageVersion.id)
            .where(models.WikiPageVersion.page_id == page.id,
                   models.WikiPageVersion.id != previous_version_id,
                   wiki_visibility.has_deleted_source(models.WikiPageVersion.id))
        ).scalars().all()
        for old_id in stale:
            original = db.get(models.WikiPageVersion, old_id)
            if original and (original.content_md or "").strip() == content:
                raise HTTPException(status_code=409, detail="版本冲突：草稿基于已失效的页面版本，请刷新后重新编辑")
    last_no = db.execute(
        select(models.WikiPageVersion.version_no).where(models.WikiPageVersion.page_id == page.id)
        .order_by(models.WikiPageVersion.version_no.desc()).limit(1)
    ).scalar() or 0
    version = models.WikiPageVersion(
        page_id=page.id, version_no=last_no + 1, content_md=content,
        summary=body.summary if body.summary is not None else page.summary,
        origin="human", note=body.note or "人工校对", created_by=user.id,
    )
    db.add(version)
    db.flush()
    # 继承被编辑版本的全部生成输入；先取旧指针，避免误用最新历史版本。
    if previous_version_id is not None:
        sources = db.execute(
            select(models.WikiGenerationSource.doc_id, models.WikiGenerationSource.doc_ref_id).where(
                models.WikiGenerationSource.page_version_id == previous_version_id
            ).distinct()
        )
        for doc_id, doc_ref_id in sources:
            db.add(models.WikiGenerationSource(
                kb_id=kb_id, page_version_id=version.id, doc_id=doc_id, doc_ref_id=doc_ref_id,
            ))
    page.current_version_id = version.id
    page.human_locked = True
    page.status = models.WikiPageStatus.published.value
    page.updated_at = models.utcnow()
    if body.summary is not None:
        page.summary = body.summary
    db.add(page)

    # 保留被编辑版本的证据关联（人工编辑不破坏来源链）。
    existing = db.execute(
        select(models.WikiEvidence).where(models.WikiEvidence.page_version_id == version.id)
    ).scalars().all()
    if previous_version_id is not None and not existing:
        for r in db.execute(
            select(models.WikiEvidence).where(models.WikiEvidence.page_version_id == previous_version_id)
        ).scalars():
            db.add(models.WikiEvidence(
                kb_id=kb_id, page_version_id=version.id, chunk_id=r.chunk_id,
                doc_id=r.doc_id, doc_version_id=r.doc_version_id,
                quote=r.quote, locator=r.locator))

    # 重建该页检索单元
    db.execute(delete(models.WikiSection).where(models.WikiSection.page_version_id == version.id))
    units = chunking.split_markdown(content)
    sig = kb.embed_signature
    vectors: list[list[float]] = []
    # 人工校对后的重建也是一次真实的模型调用，必须计入成本。
    # 之前这里同样漏传 stats：人工校对这一动作在论文里属于「人工整理时间」，
    # 但它顺带触发的向量化开销属于「机器整理成本」，两者都要能分别报出来，
    # 否则「人工校对到底省了还是多花了机器成本」这个结论无法支撑。
    cost_stats = llm.CallStats()
    try:
        vectors = llm.embed(db, [u.text for u in units], stats=cost_stats) if units else []
        if vectors:
            sig = llm.active_embedding(db)[2]
    except llm.ModelUnavailable as exc:
        cost_stats.add_note(f"章节向量化失败，仅建关键词索引：{exc}")
        vectors = []
    for unit, vec in zip(units, vectors or [None] * len(units)):
        db.add(models.WikiSection(
            kb_id=kb_id, page_id=page.id, page_version_id=version.id, ordinal=unit.ordinal,
            heading_path=unit.heading, text=unit.text,
            keywords=chunking.keyword_field(unit.text),
            vector_blob=llm.pack_vector(vec) if vec else None,
            dim=len(vec) if vec else 0, embed_signature=sig if vec else "",
        ))

    audit(db, user, "edit_wiki", target=f"{kb.name}/{page.slug}", detail=f"v{version.version_no}")
    # 人工校对触发的一次向量化也是一笔成本，入流水表。
    # 为什么必须记：论文要回答「人工校对把成本转移到哪里」——
    # 人工时间是自己花的，但重建索引仍然消耗模型调用。不记这笔，
    # 「人工校对后机器成本下降」这个结论会被系统性高估。
    cost.record(db, kb_id=kb_id, kind="wiki_edit", stats=cost_stats,
                source="wiki_edit", source_id=version.id)
    db.commit()
    return {"ok": True, "version_id": version.id, "version_no": version.version_no,
            "sections": len(units), "human_locked": True,
            # 只加字段不改旧字段：前端可据此显示「本次校对消耗的模型调用」
            "usage": cost_stats.as_dict()}


@router.get("/kbs/{kb_id}/wiki/pages/{slug}/versions")
def wiki_versions(kb_id: int, slug: str, db: Session = Depends(get_db),
                  user: models.User = Depends(get_current_user)):
    _require(db, user, kb_id)
    page = _page_for_user(db, user, kb_id, slug)
    stmt = select(models.WikiPageVersion).where(models.WikiPageVersion.page_id == page.id)
    if user.role != models.Role.admin.value:
        stmt = stmt.where(~wiki_visibility.has_deleted_source(models.WikiPageVersion.id))
    versions = db.execute(stmt.order_by(models.WikiPageVersion.version_no.desc())).scalars().all()
    # 返回正文：校对页需要能对比历史版本与当前版本，否则"版本历史"只能看到编号
    return {"items": [{
        "id": v.id, "version_no": v.version_no, "origin": v.origin, "note": v.note,
        "is_current": v.id == page.current_version_id, "chars": len(v.content_md or ""),
        "content_md": v.content_md or "",
        "summary": v.summary or "",
        "created_at": serializers.iso(v.created_at),
    } for v in versions], "total": len(versions)}


@router.post("/kbs/{kb_id}/wiki/pages/{slug}/publish")
def publish_page(kb_id: int, slug: str, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    _require(db, user, kb_id, level="manage")
    page = _page_for_user(db, user, kb_id, slug)
    if not page.current_version_id:
        raise HTTPException(status_code=400, detail="页面还没有正文版本")
    page.status = models.WikiPageStatus.published.value
    db.add(page)
    audit(db, user, "publish_wiki", target=page.slug)
    db.commit()
    return {"ok": True, "status": page.status}


@router.post("/kbs/{kb_id}/wiki/pages/{slug}/unlock")
def unlock_page(kb_id: int, slug: str, db: Session = Depends(get_db),
                user: models.User = Depends(get_current_user)):
    _require(db, user, kb_id, level="manage")
    page = _page_for_user(db, user, kb_id, slug)
    page.human_locked = False
    page.status = models.WikiPageStatus.published.value
    db.add(page)
    audit(db, user, "unlock_wiki", target=page.slug)
    db.commit()
    return {"ok": True, "human_locked": False}
