"""知识库路由门面：知识库 CRUD + 索引状态 + 重建 + 授权查询。

P1-4 把原先 933 行的单文件按职责拆开，本文件保留「知识库本体 + 门面」：

- `kbs_documents.py`：文档（上传 / 版本 / 分块 / 下载 / 删除）
- `kbs_tasks.py`：任务（列表 / 详情 / 停止 Wiki / 重试）
- `kbs_cost.py`：成本（单个知识库的累计整理成本）

三个子模块各自 `APIRouter` 后在这里聚合，路径与拆分前完全一致；文件末尾把
子模块的处理函数重导出回来，所以 `kbs.delete_document` / `kbs.retry_task` /
`kbs.get_db` / `kbs.get_current_user` 这些被测试直接调用或打桩的名字一个不少。

依赖方向单向：子模块 → `app.*` 公共服务；`kbs.py` → 子模块。子模块之间、
子模块对 `kbs.py` 都没有反向 import，故无循环依赖。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas, serializers, wiki_visibility
from ..db import get_db
from ..deps import audit, get_current_user, grant_level, require_admin
from . import kbs_cost, kbs_documents, kbs_tasks

router = APIRouter(tags=["knowledge-base"])


# ------------------------------------------------------------------ 知识库
@router.get("/kbs")
def list_kbs(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    if user.role == models.Role.admin.value:
        kbs = db.execute(select(models.KnowledgeBase).order_by(models.KnowledgeBase.id)).scalars().all()
        items = [serializers.kb_out(db, kb, my_level="manage", include_review=True) for kb in kbs]
    else:
        rows = db.execute(
            select(models.KnowledgeBase, models.KbGrant.level)
            .join(models.KbGrant, models.KbGrant.kb_id == models.KnowledgeBase.id)
            .where(models.KbGrant.user_id == user.id)
            .order_by(models.KnowledgeBase.id)
        ).all()
        items = [serializers.kb_out(db, kb, my_level=level) for kb, level in rows]
    return {"items": items, "total": len(items)}


@router.post("/kbs", status_code=201)
def create_kb(body: schemas.KbCreateIn, db: Session = Depends(get_db),
              admin: models.User = Depends(require_admin)):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="知识库名称不能为空")
    if db.execute(select(models.KnowledgeBase).where(models.KnowledgeBase.name == name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="同名知识库已存在")
    kb = models.KnowledgeBase(
        name=name, description=body.description.strip(), mode=body.mode,
        status=models.KbStatus.empty.value, status_detail="还没有文档", created_by=admin.id,
    )
    db.add(kb)
    db.flush()
    audit(db, admin, "create_kb", target=name, detail=f"mode={body.mode}")
    db.commit()
    return serializers.kb_out(db, kb, my_level="manage", include_review=True)


@router.get("/kbs/{kb_id}")
def get_kb(kb_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    level = grant_level(db, user, kb_id)
    if level is None:
        raise HTTPException(status_code=403, detail="没有该知识库的访问权限")
    data = serializers.kb_out(db, kb, my_level=level, include_review=user.role == models.Role.admin.value)
    data["index"] = _index_status(db, kb)
    return data


@router.patch("/kbs/{kb_id}")
def update_kb(kb_id: int, body: schemas.KbUpdateIn, db: Session = Depends(get_db),
              admin: models.User = Depends(require_admin)):
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="知识库名称不能为空")
        exists = db.execute(
            select(models.KnowledgeBase).where(
                models.KnowledgeBase.name == name, models.KnowledgeBase.id != kb.id)
        ).scalar_one_or_none()
        if exists:
            raise HTTPException(status_code=409, detail="同名知识库已存在")
        kb.name = name
    if body.description is not None:
        kb.description = body.description.strip()
    if body.mode is not None and body.mode != kb.mode:
        kb.mode = body.mode
        if body.mode == models.KbMode.wiki.value:
            # 先持有写锁，再检查并排队，避免并发切换重复创建首次生成任务。
            db.flush()
            has_pages = db.execute(
                select(models.WikiPage.id).where(models.WikiPage.kb_id == kb.id).limit(1)
            ).first()
            if has_pages:
                if kb.status in (models.KbStatus.ready.value, models.KbStatus.empty.value):
                    kb.status_detail = "已切换为 Wiki 增强；如需更新现有页面，请手动重建 Wiki"
            else:
                active = db.execute(select(models.Task.id).where(
                    models.Task.kb_id == kb.id,
                    models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
                ).limit(1)).first()
                has_chunks = db.execute(
                    select(models.Chunk.id).where(models.Chunk.kb_id == kb.id).limit(1)
                ).first()
                initial_key = f"{models.WIKI_INITIAL_TASK_PREFIX}{kb.id}"
                pending_initial = db.execute(select(models.Task.id).where(
                    models.Task.kb_id == kb.id, models.Task.dedupe_key == initial_key,
                    models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
                ).limit(1)).first()
                if (has_chunks or active) and not pending_initial:
                    db.add(models.Task(kind=models.TaskKind.rebuild_wiki.value,
                                       status=models.TaskStatus.queued.value, stage="排队中",
                                       kb_id=kb.id, dedupe_key=initial_key, created_by=admin.id))
                if has_chunks or active:
                    if kb.status not in (models.KbStatus.failed.value, models.KbStatus.partial.value):
                        kb.status = models.KbStatus.indexing.value
                        kb.status_detail = "已切换为 Wiki 增强，首次生成 Wiki 页面已排队"
                elif kb.status not in (models.KbStatus.failed.value, models.KbStatus.partial.value):
                    kb.status_detail = "已切换为 Wiki 增强，文档入库后将自动生成 Wiki 页面"
    db.add(kb)
    audit(db, admin, "update_kb", target=kb.name, detail=f"mode={kb.mode}")
    db.commit()
    return serializers.kb_out(db, kb, my_level="manage", include_review=True)


@router.delete("/kbs/{kb_id}")
def delete_kb(kb_id: int, db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    paths = db.execute(
        select(models.DocumentVersion.storage_path)
        .join(models.Document, models.Document.id == models.DocumentVersion.doc_id)
        .where(models.Document.kb_id == kb_id)
    ).scalars().all()
    name = kb.name
    db.delete(kb)
    audit(db, admin, "delete_kb", target=name)
    db.commit()
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True}


def _index_status(db: Session, kb: models.KnowledgeBase) -> dict:
    from ..services import llm
    cfg, provider, active_sig, active_dim = llm.active_embedding(db)
    chunk_units = db.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == kb.id)
    ).scalar() or 0
    live_versions = select(models.WikiPage.current_version_id).where(
        models.WikiPage.kb_id == kb.id,
        models.WikiPage.status != models.WikiPageStatus.draft.value,
        wiki_visibility.visible_page(),
    )
    wiki_units = db.execute(
        select(func.count(models.WikiSection.id)).where(
            models.WikiSection.kb_id == kb.id, models.WikiSection.page_version_id.in_(live_versions))
    ).scalar() or 0
    wiki_current_units = db.execute(
        select(func.count(models.WikiSection.id))
        .join(models.WikiPage, models.WikiPage.id == models.WikiSection.page_id)
        .where(models.WikiSection.kb_id == kb.id,
               models.WikiPage.status != models.WikiPageStatus.draft.value,
               models.WikiPage.current_version_id == models.WikiSection.page_version_id,
               wiki_visibility.visible_page())
    ).scalar() or 0
    pending = db.execute(
        select(func.count(models.Task.id)).where(
            models.Task.kb_id == kb.id,
            models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
        )
    ).scalar() or 0
    return {
        "chunk_ready": chunk_units > 0,
        "chunk_units": chunk_units,
        # 口径与 kb_out()/wiki_tree() 共用 serializers 里的单一判定：
        # 原来这里按 WikiSection 计数，草稿页和「发布但切不出章节」的空页
        # 都会被算成就绪，与列表页的结论相反。
        "wiki_ready": serializers.wiki_ready(db, kb.id),
        "wiki_units": wiki_units,
        "wiki_current_units": wiki_current_units,
        "embed_signature": kb.embed_signature,
        "embed_dim": kb.embed_dim,
        "active_embed_signature": active_sig,
        "active_embed_dim": active_dim,
        "signature_mismatch": bool(kb.embed_signature and kb.embed_signature != active_sig),
        "pending_tasks": pending,
    }


# ------------------------------------------------------------------ 重建
@router.post("/kbs/{kb_id}/rebuild")
def rebuild(kb_id: int, body: schemas.RebuildIn, db: Session = Depends(get_db),
            user: models.User = Depends(get_current_user)):
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if grant_level(db, user, kb_id) != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
    running = db.execute(
        select(models.Task).where(
            models.Task.kb_id == kb_id,
            models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
        )
    ).scalars().first()
    if running:
        raise HTTPException(status_code=409, detail=f"该库已有任务在执行（#{running.id}），请等待完成")

    if body.kind == "wiki":
        if kb.mode != models.KbMode.wiki.value:
            raise HTTPException(status_code=400, detail="该知识库不是 Wiki 增强模式")
        task = models.Task(
            kind=models.TaskKind.rebuild_wiki.value, status=models.TaskStatus.queued.value,
            stage="排队中", kb_id=kb_id, dedupe_key=f"wiki:{kb_id}", created_by=user.id,
        )
    else:
        if not kb.embed_signature:
            raise HTTPException(status_code=400, detail="该库还没有索引，请先上传文档")
        task = models.Task(
            kind=models.TaskKind.reindex_kb.value, status=models.TaskStatus.queued.value,
            stage="排队中", kb_id=kb_id, dedupe_key=f"reindex:{kb_id}", created_by=user.id,
        )
    db.add(task)
    kb.status = models.KbStatus.indexing.value
    kb.status_detail = "重建任务已排队"
    db.add(kb)
    db.flush()
    audit(db, user, "rebuild", target=kb.name, detail=body.kind)
    db.commit()
    return {"task_id": task.id}


@router.get("/kbs/{kb_id}/grants")
def kb_grants(kb_id: int, db: Session = Depends(get_db),
              admin: models.User = Depends(require_admin)):
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    rows = db.execute(
        select(models.KbGrant, models.User)
        .join(models.User, models.User.id == models.KbGrant.user_id)
        .where(models.KbGrant.kb_id == kb_id)
    ).all()
    return {"items": [{
        "user_id": u.id, "username": u.username, "display_name": u.display_name,
        "level": g.level, "level_label": serializers.GRANT_LABEL.get(g.level, g.level),
        "role": u.role, "status": u.status,
    } for g, u in rows]}


# ------------------------------------------------------------------ 门面聚合
# 子模块的 router 在这里挂进同一个 `kbs.router`：`main.py` 与测试都只
# `include_router(kbs.router)`，拆分不改变对外挂载方式。
router.include_router(kbs_documents.router)
router.include_router(kbs_tasks.router)
router.include_router(kbs_cost.router)

# 重导出（**不要删**）：拆分前这些名字都定义在 kbs.py 上，测试直接调
# `kbs.delete_document(...)`、`kbs.update_kb(...)`、`kbs.retry_task(...)`，
# 并用 `app.dependency_overrides[kbs.get_db]` / `[kbs.get_current_user]` 覆盖依赖。
# 保留同名绑定是「行为不变」的一部分，也保证 monkeypatch 面不缩小。
from .kbs_cost import kb_cost  # noqa: E402,F401  —— 门面重导出
from .kbs_documents import (  # noqa: E402,F401  —— 门面重导出
    AUDIT_BUSY_TIMEOUT_MS, _cleanup_staging_once, _discard_staged, _enqueue_ingest,
    _safe_audit, _stage_upload, _store_and_enqueue, _store_new_document, _store_version,
    _store_version_staged, delete_document, download_document, get_document, list_chunks,
    list_documents, log, reindex_document, upload_documents, upload_new_version,
)
from .kbs_tasks import get_task, list_tasks, retry_task, stop_task_wiki  # noqa: E402,F401
