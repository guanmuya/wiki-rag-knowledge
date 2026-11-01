"""知识库、文档、任务。"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import config, models, schemas, security, serializers
from ..db import DB_BUSY_DETAIL, apply_busy_timeout, get_db, is_lock_error
from ..deps import audit, get_current_user, grant_level, require_admin
from ..services import chunking, cost, parser, taskflow

router = APIRouter(tags=["knowledge-base"])
log = logging.getLogger("wrk.kbs")

# 审计是 best-effort：给它一个很短的等锁时间，别让它把请求总耗时翻倍。
AUDIT_BUSY_TIMEOUT_MS = 500


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
        # 口径与 kb_out()/wiki_tree() 共用 serializers 里的单一判定：
        # 原来这里按 WikiSection 计数，草稿页和「发布但切不出章节」的空页
        # 都会被算成就绪，与列表页的结论相反。
        "wiki_ready": serializers.wiki_ready(db, kb.id),
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
    lock_seen = False
    for upload in files:
        filename = Path(upload.filename or "未命名").name
        suffix = parser.sniff_suffix(filename)
        if suffix not in config.ALLOWED_SUFFIXES:
            errors.append({"filename": filename, "error": f"不支持的文件类型：{suffix or '无扩展名'}"})
            continue
        data = await upload.read()          # 读请求体不碰数据库
        if not data:
            errors.append({"filename": filename, "error": "文件为空"})
            continue
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            errors.append({"filename": filename, "error": f"超过 {config.MAX_UPLOAD_MB}MB 上限"})
            continue
        try:
            # 每个文件独立提交，并放到线程池里执行，两个原因都是真实故障：
            # 1) 旧实现一个事务横跨整批文件：第 5 个文件的落盘和 sha256 也算在
            #    写锁里，期间所有交互请求都在等锁；一个文件失败还会把前面已经
            #    登记好的记录一起回滚掉。
            # 2) 这是 async 端点，同步 DB 调用留在事件循环上会直接卡住整个服务
            #    ——包括 GET /api/kbs 这种纯读接口，前端看到的就是「软件卡死」。
            items.append(await run_in_threadpool(
                _store_and_enqueue, db, kb, filename, suffix, data,
                upload.content_type or "", user))
        except HTTPException as exc:
            db.rollback()
            errors.append({"filename": filename, "error": exc.detail})
            continue
        except OperationalError as exc:
            db.rollback()
            if not is_lock_error(exc):
                raise
            # 写锁等不到是环境问题：如实说「忙」，让用户稍后重试，
            # 而不是让界面等满超时再弹一句 500 Internal Server Error。
            lock_seen = True
            log.warning("上传 %s 时写库被锁挡住：%s", filename, getattr(exc, "orig", exc))
            errors.append({"filename": filename, "error": DB_BUSY_DETAIL})
            continue
    if lock_seen:
        # 已经为业务写入等过一次写锁了，别再为审计等第二次——
        # 否则用户感知到的是「上传卡了 6 秒」，比原来的问题更糟。
        log.warning("数据库繁忙，跳过本次上传审计（ok=%d fail=%d）", len(items), len(errors))
    else:
        _safe_audit(db, user, "upload_documents", target=kb.name,
                    detail=f"ok={len(items)} fail={len(errors)}")
    return {"items": items, "errors": errors}


def _store_and_enqueue(db: Session, kb: models.KnowledgeBase, filename: str, suffix: str,
                       data: bytes, mime: str, user: models.User) -> dict:
    """登记一个文件的完整流程（落盘 → 版本 → 排队），单独提交。"""
    doc, version = _store_new_document(db, kb, filename, suffix, data, mime, user)
    task = _enqueue_ingest(db, kb, doc, version, user)
    db.commit()
    return {"doc_id": doc.id, "version_id": version.id, "task_id": task.id, "filename": filename}


def _safe_audit(db: Session, user: models.User | None, action: str, *,
                target: str = "", detail: str = "") -> None:
    """写审计；写不进去只记日志，不让整个请求失败。

    上传这种「文件已经落盘、任务已经排队」的动作，不能因为一行审计被写锁挡住
    就整体报错——用户会以为没传上去，重传一次反而多出一条版本。
    等待时间也压短：审计是「有最好」的记录，不值得让用户再等一个 busy_timeout。
    """
    try:
        apply_busy_timeout(db, AUDIT_BUSY_TIMEOUT_MS)
        audit(db, user, action, target=target, detail=detail)
        db.commit()
    except OperationalError as exc:
        db.rollback()
        if not is_lock_error(exc):
            raise
        log.warning("审计写入被写锁挡住，已跳过（%s/%s）：%s", action, target, exc)


def _stage_upload(data: bytes, suffix: str) -> tuple[Path, str]:
    """把上传内容写进暂存目录并算校验和——都在写事务之外完成。

    为什么先暂存：最终路径带 `doc.id`，新文档必须先 INSERT 拿到 id 才知道路径。
    旧实现在 flush 之后写 8MB 文件、算整份 sha256，写锁被文件 I/O 占住几十到
    上百毫秒；一次传 5 个文件时更久，期间所有写请求（提问落库、建会话、
    另一个 worker 抢任务）都在等锁。现在事务里只剩一次同盘 rename。
    """
    staging = Path(config.FILES_DIR) / "_staging"
    staging.mkdir(parents=True, exist_ok=True)
    _cleanup_staging_once(staging)
    staged = staging / f"{uuid.uuid4().hex}{suffix}"
    staged.write_bytes(data)
    return staged, security.checksum_file(staged)


_staging_cleaned = False


def _cleanup_staging_once(staging: Path) -> None:
    """进程内第一次上传时，清一遍暂存目录里一天前的残留。

    桌面版会被用户强关（正是本 bug 的现场），进程死在「已落盘、还没入库」
    之间就会留下一个没有任何引用的暂存文件。只扫一次、只删 24 小时前的，
    既不会误删正在上传的文件，也不给热路径添负担。
    """
    global _staging_cleaned
    if _staging_cleaned:
        return
    _staging_cleaned = True
    cutoff = time.time() - 24 * 3600
    try:
        for item in staging.iterdir():
            try:
                if item.is_file() and item.stat().st_mtime < cutoff:
                    item.unlink(missing_ok=True)
            except OSError:
                continue
    except OSError:  # pragma: no cover - 目录不可读
        pass


def _discard_staged(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 文件已被挪走/权限问题
        pass


def _store_new_document(db: Session, kb: models.KnowledgeBase, filename: str, suffix: str,
                        data: bytes, mime: str, user: models.User) -> tuple[models.Document, models.DocumentVersion]:
    staged, checksum = _stage_upload(data, suffix)
    try:
        doc = db.execute(
            select(models.Document).where(
                models.Document.kb_id == kb.id,
                models.Document.filename == filename,
                models.Document.deleted.is_(False),
            )
        ).scalar_one_or_none()
        if doc:
            return _store_version_staged(db, doc, staged, checksum, len(data), user, mime)

        doc = models.Document(
            kb_id=kb.id, filename=filename, suffix=suffix, size_bytes=len(data), mime=mime,
            status=models.KbStatus.indexing.value, status_detail="等待处理", created_by=user.id,
        )
        db.add(doc)
        db.flush()      # 只为拿 doc.id 拼存储路径，写锁停留毫秒级
        return _store_version_staged(db, doc, staged, checksum, len(data), user, mime)
    except Exception:
        _discard_staged(staged)
        raise


def _store_version(db: Session, doc: models.Document, data: bytes, user: models.User,
                   mime: str = "") -> tuple[models.Document, models.DocumentVersion]:
    """给文档加一个新版本：先在事务外落盘 + 算校验和，再进写事务。"""
    staged, checksum = _stage_upload(data, doc.suffix)
    return _store_version_staged(db, doc, staged, checksum, len(data), user, mime)


def _store_version_staged(db: Session, doc: models.Document, staged: Path, checksum: str,
                          size: int, user: models.User,
                          mime: str = "") -> tuple[models.Document, models.DocumentVersion]:
    doc_dir = Path(config.FILES_DIR) / str(doc.kb_id) / str(doc.id)
    stored = doc_dir / staged.name
    moved = False
    try:
        doc_dir.mkdir(parents=True, exist_ok=True)
        os.replace(staged, stored)      # 同盘 rename：写事务里只剩这一步
        moved = True
        last = db.execute(
            select(func.max(models.DocumentVersion.version_no)).where(models.DocumentVersion.doc_id == doc.id)
        ).scalar() or 0
        version = models.DocumentVersion(
            doc_id=doc.id, version_no=last + 1, storage_path=str(stored), checksum=checksum,
            status=models.TaskStatus.queued.value, created_by=user.id,
        )
        db.add(version)
        db.flush()
        doc.size_bytes = size
        doc.status = models.KbStatus.indexing.value
        doc.status_detail = "等待处理"
        if mime:
            doc.mime = mime
        db.add(doc)
        db.flush()
        return doc, version
    except Exception:
        # 事务没成：把已经挪过去的文件删掉，别在文件目录里留孤儿
        _discard_staged(stored if moved else staged)
        raise


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

    def _write() -> dict:
        # 落盘 + 校验和在事务外，写事务只覆盖几行 INSERT（同上传路径的取舍）；
        # 同步 DB 调用放线程池，避免卡住事件循环。
        _, version = _store_version(db, doc, data, user, file.content_type or "")
        kb = db.get(models.KnowledgeBase, doc.kb_id)
        task = _enqueue_ingest(db, kb, doc, version, user)
        audit(db, user, "upload_version", target=doc.filename, detail=f"v{version.version_no}")
        db.commit()
        return {"version_id": version.id, "task_id": task.id, "version_no": version.version_no}

    try:
        return await run_in_threadpool(_write)
    except OperationalError as exc:
        db.rollback()
        if not is_lock_error(exc):
            raise
        log.warning("上传新版本 %s 时写库被锁挡住：%s", doc.filename, getattr(exc, "orig", exc))
        raise HTTPException(status_code=503, detail=DB_BUSY_DETAIL) from exc


@router.post("/documents/{doc_id}/reindex")
def reindex_document(doc_id: int, db: Session = Depends(get_db),
                     user: models.User = Depends(get_current_user)):
    """重试这份文档的入库（解析 → 分块 → 索引）。

    为什么重试放在文档上而不是任务上：用户关心的是「这份文档能不能用」，
    不是「哪一次任务记录失败了」。文档列表点重试，这里找到该文档最新版本
    对应的那条入库任务原地重排；没有任务记录（历史数据）才补建一条。
    这样任务列表里始终是「一份文档一条记录」，点多少次都不会堆。
    """
    doc = db.get(models.Document, doc_id)
    if not doc or doc.deleted:
        raise HTTPException(status_code=404, detail="文档不存在")
    if grant_level(db, user, doc.kb_id) != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")

    version = taskflow.latest_version(db, doc.id)
    if not version:
        raise HTTPException(status_code=400, detail="该文档还没有可重试的版本")
    tasks = taskflow.ingest_tasks_for_version(db, version.id)
    active = taskflow.active_task(tasks)
    if active:
        # 旧版重试接口会为同一份文档留下多条 queued 任务。这里顺手合并：
        # 保留最新那条，其余标失败并写明原因——它们还没执行，合并不会丢任何工作，
        # 也不会让同一份文档被重复解析。
        merged = taskflow.collapse_queued_duplicates(db, tasks, active)
        if merged:
            hist = taskflow.retry_history(active)
            taskflow.sync_running(db, active, detail="重复任务已合并，等待处理")
            audit(db, user, "merge_duplicate_tasks", target=doc.filename,
                  detail=f"kept=#{active.id} merged={merged}")
            db.commit()
            return {
                "task_id": active.id, "version_id": version.id, "reused": True,
                "retry_count": int(hist.get("count") or 0), "merged": merged,
            }
        raise HTTPException(
            status_code=409,
            detail=f"该文档已在处理中（任务 #{active.id}），无需重试",
        )

    task = tasks[0] if tasks else None
    reused = task is not None
    if task is None:
        task = models.Task(
            kind=models.TaskKind.ingest_document.value, status=models.TaskStatus.queued.value,
            stage="排队中", progress=0, kb_id=doc.kb_id, doc_id=doc.id,
            doc_version_id=version.id, dedupe_key=f"ingest:{version.id}",
            payload=json.dumps({"doc": doc.filename}, ensure_ascii=False),
            created_by=user.id,
        )
        db.add(task)
        db.flush()
        hist: dict = {"count": 0}
    else:
        hist = taskflow.requeue(db, task)

    taskflow.sync_running(db, task, detail=f"已重新排队（第 {hist.get('count', 1)} 次）")
    audit(db, user, "retry_document", target=doc.filename,
          detail=f"version=v{version.version_no} task={task.id}")
    db.commit()
    return {
        "task_id": task.id, "version_id": version.id, "reused": reused,
        "retry_count": hist.get("count", 0), "merged": 0,
    }


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
    """把一条已结束的任务原地打回队列（不新建任务行）。

    旧实现每次都 `INSERT` 一条新任务：用户点三次重试，任务列表就多三条，
    同一份文档还会被并发解析三遍。现在重试 = 同一条记录的状态回退，
    「重试了几次」记在 payload.retry.count 上，同时把文档/版本/知识库
    状态同步回「索引中」——任务列表与文档列表不再各说各话。
    """
    task = db.get(models.Task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.kb_id and grant_level(db, user, task.kb_id) != models.GrantLevel.manage.value:
        raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
    if task.status in (models.TaskStatus.queued.value, models.TaskStatus.running.value):
        raise HTTPException(status_code=409, detail="任务已在队列中或正在执行，无需重试")
    hist = taskflow.requeue(db, task)
    taskflow.sync_running(db, task)
    audit(db, user, "retry_task", target=str(task.id),
          detail=f"kind={task.kind} retry={hist.get('count', 0)}")
    db.commit()
    return {"task_id": task.id, "reused": True, "retry_count": hist.get("count", 0)}


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
