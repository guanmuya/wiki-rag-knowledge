"""知识库、文档、任务。"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import config, models, schemas, security, serializers
from ..db import get_db
from ..deps import audit, get_current_user, grant_level, require_admin
from ..services import chunking, cost, parser

router = APIRouter(tags=["knowledge-base"])


# ------------------------------------------------------------------ 知识库
@router.get("/kbs")
def list_kbs(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    if user.role == models.Role.admin.value:
        kbs = db.execute(select(models.KnowledgeBase).order_by(models.KnowledgeBase.id)).scalars().all()
        items = [serializers.kb_out(db, kb, my_level="manage") for kb in kbs]
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
    return serializers.kb_out(db, kb, my_level="manage")


@router.get("/kbs/{kb_id}")
def get_kb(kb_id: int, db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    level = grant_level(db, user, kb_id)
    if level is None:
        raise HTTPException(status_code=403, detail="没有该知识库的访问权限")
    data = serializers.kb_out(db, kb, my_level=level)
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
            kb.status_detail = "已切换为 Wiki 增强，需重建 Wiki 后才会生成页面"
    db.add(kb)
    audit(db, admin, "update_kb", target=kb.name, detail=f"mode={kb.mode}")
    db.commit()
    return serializers.kb_out(db, kb, my_level="manage")


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
    wiki_units = db.execute(
        select(func.count(models.WikiSection.id)).where(models.WikiSection.kb_id == kb.id)
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
        "wiki_ready": wiki_units > 0,
        "wiki_units": wiki_units,
        "embed_signature": kb.embed_signature,
        "embed_dim": kb.embed_dim,
        "active_embed_signature": active_sig,
        "active_embed_dim": active_dim,
        "signature_mismatch": bool(kb.embed_signature and kb.embed_signature != active_sig),
        "pending_tasks": pending,
    }


# ------------------------------------------------------------------ 文档
@router.get("/kbs/{kb_id}/documents")
def list_documents(kb_id: int, db: Session = Depends(get_db),
                   user: models.User = Depends(get_current_user)):
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    if grant_level(db, user, kb_id) is None:
        raise HTTPException(status_code=403, detail="没有该知识库的访问权限")
    docs = db.execute(
        select(models.Document).where(
            models.Document.kb_id == kb_id, models.Document.deleted.is_(False)
        ).order_by(models.Document.id.desc())
    ).scalars().all()
    return {"items": [serializers.doc_out(db, d) for d in docs], "total": len(docs)}


@router.post("/kbs/{kb_id}/documents")
async def upload_documents(
    kb_id: int,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: models.User = Depends(get_current_user),
):
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    level = grant_level(db, user, kb_id)
    if level != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限才能上传")

    items, errors = [], []
    for upload in files:
        filename = Path(upload.filename or "未命名").name
        suffix = parser.sniff_suffix(filename)
        if suffix not in config.ALLOWED_SUFFIXES:
            errors.append({"filename": filename, "error": f"不支持的文件类型：{suffix or '无扩展名'}"})
            continue
        data = await upload.read()
        if not data:
            errors.append({"filename": filename, "error": "文件为空"})
            continue
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            errors.append({"filename": filename, "error": f"超过 {config.MAX_UPLOAD_MB}MB 上限"})
            continue
        try:
            doc, version = _store_new_document(db, kb, filename, suffix, data, upload.content_type or "", user)
        except HTTPException as exc:
            errors.append({"filename": filename, "error": exc.detail})
            continue
        task = _enqueue_ingest(db, kb, doc, version, user)
        items.append({"doc_id": doc.id, "version_id": version.id, "task_id": task.id,
                      "filename": filename})
    audit(db, user, "upload_documents", target=kb.name,
          detail=f"ok={len(items)} fail={len(errors)}")
    db.commit()
    return {"items": items, "errors": errors}


def _store_new_document(db: Session, kb: models.KnowledgeBase, filename: str, suffix: str,
                        data: bytes, mime: str, user: models.User) -> tuple[models.Document, models.DocumentVersion]:
    doc = db.execute(
        select(models.Document).where(
            models.Document.kb_id == kb.id,
            models.Document.filename == filename,
            models.Document.deleted.is_(False),
        )
    ).scalar_one_or_none()
    if doc:
        return _store_version(db, doc, data, user, mime)

    doc = models.Document(
        kb_id=kb.id, filename=filename, suffix=suffix, size_bytes=len(data), mime=mime,
        status=models.KbStatus.indexing.value, status_detail="等待处理", created_by=user.id,
    )
    db.add(doc)
    db.flush()
    _, version = _store_version(db, doc, data, user, mime)
    return doc, version


def _store_version(db: Session, doc: models.Document, data: bytes, user: models.User,
                   mime: str = "") -> tuple[models.Document, models.DocumentVersion]:
    doc_dir = Path(config.FILES_DIR) / str(doc.kb_id) / str(doc.id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    stored = doc_dir / f"{uuid.uuid4().hex}{doc.suffix}"
    stored.write_bytes(data)
    checksum = security.checksum_file(stored)
    last = db.execute(
        select(func.max(models.DocumentVersion.version_no)).where(models.DocumentVersion.doc_id == doc.id)
    ).scalar() or 0
    version = models.DocumentVersion(
        doc_id=doc.id, version_no=last + 1, storage_path=str(stored), checksum=checksum,
        status=models.TaskStatus.queued.value, created_by=user.id,
    )
    db.add(version)
    db.flush()
    doc.size_bytes = len(data)
    doc.status = models.KbStatus.indexing.value
    doc.status_detail = "等待处理"
    if mime:
        doc.mime = mime
    db.add(doc)
    db.flush()
    return doc, version


def _enqueue_ingest(db: Session, kb: models.KnowledgeBase, doc: models.Document,
                    version: models.DocumentVersion, user: models.User) -> models.Task:
    dedupe = f"ingest:{version.id}"
    existing = db.execute(
        select(models.Task).where(
            models.Task.dedupe_key == dedupe,
            models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
        )
    ).scalars().first()
    if existing:
        return existing
    task = models.Task(
        kind=models.TaskKind.ingest_document.value, status=models.TaskStatus.queued.value,
        stage="排队中", progress=0, kb_id=kb.id, doc_id=doc.id, doc_version_id=version.id,
        dedupe_key=dedupe, payload=json.dumps({"doc": doc.filename}, ensure_ascii=False),
        created_by=user.id,
    )
    db.add(task)
    db.flush()
    kb.status = models.KbStatus.indexing.value
    kb.status_detail = "正在处理文档"
    db.add(kb)
    return task


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, db: Session = Depends(get_db),
                 user: models.User = Depends(get_current_user)):
    doc = db.get(models.Document, doc_id)
    if not doc or doc.deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    if grant_level(db, user, doc.kb_id) is None:
        raise HTTPException(status_code=403, detail="没有该文档的访问权限")
    data = serializers.doc_out(db, doc)
    versions = db.execute(
        select(models.DocumentVersion).where(models.DocumentVersion.doc_id == doc_id)
        .order_by(models.DocumentVersion.version_no.desc())
    ).scalars().all()
    data["versions"] = [{
        "id": v.id, "version_no": v.version_no, "status": v.status,
        "status_label": serializers.TASK_STATUS_LABEL.get(v.status, v.status),
        "status_detail": v.status_detail, "chunk_count": v.chunk_count,
        "text_chars": v.text_chars, "checksum": v.checksum[:12],
        "created_at": serializers.iso(v.created_at),
    } for v in versions]
    tasks = db.execute(
        select(models.Task).where(models.Task.doc_id == doc_id).order_by(models.Task.id.desc()).limit(10)
    ).scalars().all()
    data["tasks"] = [serializers.task_out(db, t) for t in tasks]
    return data


@router.post("/documents/{doc_id}/versions", status_code=201)
async def upload_new_version(doc_id: int, file: UploadFile = File(...),
                             db: Session = Depends(get_db),
                             user: models.User = Depends(get_current_user)):
    doc = db.get(models.Document, doc_id)
    if not doc or doc.deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    if grant_level(db, user, doc.kb_id) != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
    filename = Path(file.filename or doc.filename).name
    suffix = parser.sniff_suffix(filename)
    if suffix not in config.ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{suffix or '无扩展名'}")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="文件为空")
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(status_code=400, detail=f"超过 {config.MAX_UPLOAD_MB}MB 上限")
    if suffix != doc.suffix:
        doc.suffix = suffix
    _, version = _store_version(db, doc, data, user, file.content_type or "")
    kb = db.get(models.KnowledgeBase, doc.kb_id)
    task = _enqueue_ingest(db, kb, doc, version, user)
    audit(db, user, "upload_version", target=doc.filename, detail=f"v{version.version_no}")
    db.commit()
    return {"version_id": version.id, "task_id": task.id, "version_no": version.version_no}


@router.get("/documents/{doc_id}/download")
def download_document(doc_id: int, version_id: int | None = Query(default=None),
                      db: Session = Depends(get_db),
                      user: models.User = Depends(get_current_user)):
    doc = db.get(models.Document, doc_id)
    if not doc or doc.deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    if grant_level(db, user, doc.kb_id) is None:
        raise HTTPException(status_code=403, detail="没有该文档的访问权限")
    version = db.get(models.DocumentVersion, version_id) if version_id else (
        db.get(models.DocumentVersion, doc.current_version_id) if doc.current_version_id else None)
    if not version or version.doc_id != doc.id:
        raise HTTPException(status_code=404, detail="版本不存在")
    path = Path(version.storage_path)
    if not path.exists():
        raise HTTPException(status_code=410, detail="原文件已不存在")
    audit(db, user, "download", target=doc.filename, detail=f"v{version.version_no}")
    db.commit()
    return FileResponse(path, filename=f"{Path(doc.filename).stem}_v{version.version_no}{doc.suffix}")


@router.get("/documents/{doc_id}/chunks")
def list_chunks(doc_id: int, page: int = 1, page_size: int = 20,
                db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    doc = db.get(models.Document, doc_id)
    if not doc or doc.deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    if grant_level(db, user, doc.kb_id) is None:
        raise HTTPException(status_code=403, detail="没有该文档的访问权限")
    version_id = doc.current_version_id
    if not version_id:
        return {"items": [], "total": 0, "page": page, "page_size": page_size}
    base = select(models.Chunk).where(models.Chunk.doc_version_id == version_id)
    total = db.execute(
        select(func.count(models.Chunk.id)).where(models.Chunk.doc_version_id == version_id)
    ).scalar() or 0
    rows = db.execute(
        base.order_by(models.Chunk.ordinal).offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {
        "items": [{
            "id": c.id, "ordinal": c.ordinal, "text": c.text, "heading": c.heading,
            "locator": c.locator, "dim": c.dim, "embed_signature": c.embed_signature,
            "has_vector": bool(c.vector_blob),
            "keywords": (c.keywords or "").split(" ")[:12],
        } for c in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    doc = db.get(models.Document, doc_id)
    if not doc or doc.deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = db.get(models.KnowledgeBase, doc.kb_id)
    if grant_level(db, user, doc.kb_id) != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
    paths = db.execute(
        select(models.DocumentVersion.storage_path).where(models.DocumentVersion.doc_id == doc.id)
    ).scalars().all()
    db.execute(models.Chunk.__table__.delete().where(models.Chunk.doc_id == doc.id))
    doc.deleted = True
    doc.status = models.KbStatus.empty.value
    doc.status_detail = "已删除"
    db.add(doc)
    task_id = None
    if kb and kb.mode == models.KbMode.wiki.value:
        task = models.Task(
            kind=models.TaskKind.rebuild_wiki.value, status=models.TaskStatus.queued.value,
            stage="排队中", kb_id=kb.id, doc_id=doc.id,
            dedupe_key=f"wiki:{kb.id}", payload="{}", created_by=user.id,
        )
        db.add(task)
        db.flush()
        task_id = task.id
    audit(db, user, "delete_document", target=doc.filename)
    db.commit()
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except OSError:
            pass
    return {"ok": True, "rebuild_task_id": task_id}


# ------------------------------------------------------------------ 任务
@router.get("/tasks")
def list_tasks(status: str | None = None, kb_id: int | None = None, limit: int = 50,
               db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    stmt = select(models.Task).order_by(models.Task.id.desc()).limit(min(limit, 200))
    if status:
        wanted = [s.strip() for s in status.split(",") if s.strip()]
        stmt = stmt.where(models.Task.status.in_(wanted))
    if kb_id:
        stmt = stmt.where(models.Task.kb_id == kb_id)
    tasks = db.execute(stmt).scalars().all()
    if user.role != models.Role.admin.value:
        allowed = {g.kb_id for g in db.execute(
            select(models.KbGrant).where(models.KbGrant.user_id == user.id)).scalars()}
        tasks = [t for t in tasks if t.kb_id in allowed]
    return {"items": [serializers.task_out(db, t) for t in tasks], "total": len(tasks)}


@router.get("/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db),
             user: models.User = Depends(get_current_user)):
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.kb_id and grant_level(db, user, task.kb_id) is None:
        raise HTTPException(status_code=403, detail="没有该任务的访问权限")
    return serializers.task_out(db, task)


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, db: Session = Depends(get_db),
               user: models.User = Depends(get_current_user)):
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.kb_id and grant_level(db, user, task.kb_id) != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
    if task.status == models.TaskStatus.running.value:
        raise HTTPException(status_code=409, detail="任务正在执行中")
    retry = models.Task(
        kind=task.kind, status=models.TaskStatus.queued.value, stage="排队中", progress=0,
        kb_id=task.kb_id, doc_id=task.doc_id, doc_version_id=task.doc_version_id,
        dedupe_key=f"retry:{task.id}:{models.utcnow().timestamp():.0f}",
        payload=task.payload, created_by=user.id,
    )
    db.add(retry)
    db.flush()
    audit(db, user, "retry_task", target=str(task.id), detail=f"kind={task.kind}")
    db.commit()
    return {"task_id": retry.id}


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


@router.get("/kbs/{kb_id}/cost")
def kb_cost(kb_id: int, db: Session = Depends(get_db),
            user: models.User = Depends(get_current_user)):
    """该知识库的累计整理成本（模型调用量 / token / 耗时）。

    权限：沿用 grant_level，read 即可查看——成本是「这个库整理花了多少」的
    说明性数据，不是管理动作；但看不到库的人也不该看到它的成本构成，
    否则可以借此推断别部门的知识库规模。

    为什么不做成 admin-only：论文实验用的是普通员工账号跑问答，
    如果只有管理员能读成本，采集脚本就得持管理员凭据，
    权限模型会被实验流程倒逼放宽，这是更差的结果。
    """
    kb = db.get(models.KnowledgeBase, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    level = grant_level(db, user, kb_id)
    if level is None:
        raise HTTPException(status_code=403, detail="没有该知识库的访问权限")
    data = cost.kb_summary(db, kb)
    data["my_level"] = level
    return data


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
