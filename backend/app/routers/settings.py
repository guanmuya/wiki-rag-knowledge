"""模型设置：供应商、模型、连接测试、Embedding 切换与审计。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas, security, serializers
from ..db import get_db
from ..deps import audit, require_admin
from ..services import llm

router = APIRouter(tags=["settings"])


# ------------------------------------------------------------------ 供应商
@router.get("/settings/providers")
def list_providers(db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    rows = db.execute(select(models.Provider).order_by(models.Provider.id)).scalars().all()
    return {"items": [serializers.provider_out(p) for p in rows], "total": len(rows)}


@router.post("/settings/providers", status_code=201)
def create_provider(body: schemas.ProviderIn, db: Session = Depends(get_db),
                    admin: models.User = Depends(require_admin)):
    name = body.name.strip()
    if db.execute(select(models.Provider).where(models.Provider.name == name)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="同名供应商已存在")
    provider = models.Provider(
        name=name, base_url=body.base_url.strip().rstrip("/"),
        api_key_enc=security.encrypt_secret(body.api_key.strip()), enabled=body.enabled,
        protocol=body.protocol,
    )
    db.add(provider)
    audit(db, admin, "create_provider", target=name, detail=body.protocol)
    db.commit()
    return serializers.provider_out(provider)


@router.patch("/settings/providers/{pid}")
def update_provider(pid: int, body: schemas.ProviderUpdateIn, db: Session = Depends(get_db),
                    admin: models.User = Depends(require_admin)):
    provider = db.get(models.Provider, pid)
    if not provider:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if body.name is not None:
        provider.name = body.name.strip()
    if body.base_url is not None:
        provider.base_url = body.base_url.strip().rstrip("/")
    if body.enabled is not None:
        provider.enabled = body.enabled
    if body.protocol is not None:
        provider.protocol = body.protocol
    if body.api_key is not None:
        key = body.api_key.strip()
        if key == "__clear__":
            provider.api_key_enc = ""
        elif key:
            provider.api_key_enc = security.encrypt_secret(key)
    db.add(provider)
    audit(db, admin, "update_provider", target=provider.name,
          detail="key_changed" if body.api_key else "")
    db.commit()
    return serializers.provider_out(provider)


@router.post("/settings/providers/{pid}/discover")
def discover_provider_models(pid: int, db: Session = Depends(get_db),
                             admin: models.User = Depends(require_admin)):
    """探测供应商侧可用模型。

    永远返回 200 + 结果对象：探测失败是正常业务状态（很多中转没有 /models），
    前端要能显示「探测不到，请手动填写」，而不是弹一个 500 错误页。
    """
    provider = db.get(models.Provider, pid)
    if not provider:
        raise HTTPException(status_code=404, detail="供应商不存在")
    result = llm.discover_models(db, provider)
    audit(db, admin, "discover_models", target=provider.name,
          detail=f"source={result['source']} total={result['total']}"
                 + (f" error={result['error'][:120]}" if result["error"] else ""))
    db.commit()
    return result


@router.post("/settings/providers/{pid}/models/import")
def import_provider_models(pid: int, body: schemas.ModelImportIn,
                           db: Session = Depends(get_db),
                           admin: models.User = Depends(require_admin)):
    """批量导入模型配置，幂等：已存在的 (provider, purpose, model) 跳过。"""
    provider = db.get(models.Provider, pid)
    if not provider:
        raise HTTPException(status_code=404, detail="供应商不存在")
    if not body.items:
        return {"created": [], "skipped": [], "created_count": 0, "skipped_count": 0}
    created, skipped = llm.import_models(
        db, provider, [i.model_dump() for i in body.items])
    audit(db, admin, "import_models", target=provider.name,
          detail=f"created={len(created)} skipped={len(skipped)}")
    db.commit()
    return {"created": created, "skipped": skipped,
            "created_count": len(created), "skipped_count": len(skipped)}


@router.delete("/settings/providers/{pid}")
def delete_provider(pid: int, db: Session = Depends(get_db),
                    admin: models.User = Depends(require_admin)):
    provider = db.get(models.Provider, pid)
    if not provider:
        raise HTTPException(status_code=404, detail="供应商不存在")
    in_use = db.execute(
        select(func.count(models.ModelConfig.id)).where(
            models.ModelConfig.provider_id == pid, models.ModelConfig.is_default.is_(True))
    ).scalar() or 0
    if in_use:
        raise HTTPException(status_code=409, detail="该供应商下有默认模型，请先调整默认模型")
    name = provider.name
    db.delete(provider)
    audit(db, admin, "delete_provider", target=name)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 模型
@router.get("/settings/models")
def list_models(db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    rows = db.execute(select(models.ModelConfig).order_by(
        models.ModelConfig.purpose, models.ModelConfig.id)).scalars().all()
    return {"items": [serializers.model_out(db, m) for m in rows], "total": len(rows)}


@router.post("/settings/models", status_code=201)
def create_model(body: schemas.ModelIn, db: Session = Depends(get_db),
                 admin: models.User = Depends(require_admin)):
    if not db.get(models.Provider, body.provider_id):
        raise HTTPException(status_code=400, detail="供应商不存在")
    if body.is_default:
        _clear_default(db, body.purpose)
    cfg = models.ModelConfig(
        provider_id=body.provider_id, purpose=body.purpose, model=body.model.strip(),
        label=body.label.strip() or body.model.strip(),
        params=json.dumps(body.params, ensure_ascii=False),
        is_default=body.is_default, enabled=body.enabled, embed_dim=body.embed_dim,
    )
    db.add(cfg)
    audit(db, admin, "create_model", target=cfg.model, detail=body.purpose)
    db.commit()
    return serializers.model_out(db, cfg)


@router.patch("/settings/models/{mid}")
def update_model(mid: int, body: schemas.ModelUpdateIn, db: Session = Depends(get_db),
                 admin: models.User = Depends(require_admin)):
    cfg = db.get(models.ModelConfig, mid)
    if not cfg:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    if body.is_default:
        _clear_default(db, cfg.purpose, keep_id=cfg.id)
    if body.label is not None:
        cfg.label = body.label.strip()
    if body.model is not None:
        if cfg.purpose == models.ModelPurpose.embedding.value and _has_any_index(db):
            if body.model.strip() != cfg.model:
                raise HTTPException(
                    status_code=409,
                    detail="已有索引时不能直接修改 Embedding 模型标识，请使用「更换模型并全量重建」")
        cfg.model = body.model.strip()
    if body.params is not None:
        cfg.params = json.dumps(body.params, ensure_ascii=False)
    if body.is_default is not None:
        cfg.is_default = body.is_default
    if body.enabled is not None:
        cfg.enabled = body.enabled
    if body.embed_dim is not None:
        cfg.embed_dim = body.embed_dim
    db.add(cfg)
    audit(db, admin, "update_model", target=cfg.model)
    db.commit()
    return serializers.model_out(db, cfg)


@router.delete("/settings/models/{mid}")
def delete_model(mid: int, db: Session = Depends(get_db),
                 admin: models.User = Depends(require_admin)):
    cfg = db.get(models.ModelConfig, mid)
    if not cfg:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    if cfg.purpose == models.ModelPurpose.embedding.value and _has_any_index(db):
        raise HTTPException(status_code=409,
                            detail="已有索引时不能删除当前 Embedding 配置，请先更换并重建")
    model_name = cfg.model
    db.delete(cfg)
    audit(db, admin, "delete_model", target=model_name)
    db.commit()
    return {"ok": True}


@router.post("/settings/models/{mid}/test")
def test_model(mid: int, db: Session = Depends(get_db),
               admin: models.User = Depends(require_admin)):
    cfg = db.get(models.ModelConfig, mid)
    if not cfg:
        raise HTTPException(status_code=404, detail="模型配置不存在")
    result = llm.test_model(db, cfg)
    audit(db, admin, "test_model", target=cfg.model,
          detail=f"ok={result['ok']} {result['message'][:200]}")
    db.commit()
    return result


def _clear_default(db: Session, purpose: str, keep_id: int | None = None) -> None:
    rows = db.execute(
        select(models.ModelConfig).where(
            models.ModelConfig.purpose == purpose, models.ModelConfig.is_default.is_(True))
    ).scalars().all()
    for r in rows:
        if keep_id and r.id == keep_id:
            continue
        r.is_default = False
        db.add(r)


def _has_any_index(db: Session) -> bool:
    return bool(db.execute(select(models.Chunk.id).limit(1)).first())


# ------------------------------------------------------------------ Embedding 状态与切换
@router.get("/settings/embedding/status")
def embedding_status(db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    cfg, provider, sig, dim = llm.active_embedding(db)
    kbs = db.execute(select(models.KnowledgeBase).order_by(models.KnowledgeBase.id)).scalars().all()
    kb_rows = []
    for kb in kbs:
        chunk_count = db.execute(
            select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == kb.id)
        ).scalar() or 0
        kb_rows.append({
            "kb_id": kb.id, "kb_name": kb.name, "mode": kb.mode,
            "embed_signature": kb.embed_signature, "embed_dim": kb.embed_dim,
            "chunk_count": chunk_count,
            "mismatch": bool(kb.embed_signature and kb.embed_signature != sig),
        })
    running = db.execute(
        select(models.Task).where(
            models.Task.kind.in_([models.TaskKind.embedding_rebuild.value, models.TaskKind.reindex_kb.value]),
            models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
        )
    ).scalars().all()
    return {
        "active": {
            "model_config_id": cfg.id if cfg else None,
            "provider": provider.name if provider else "",
            "model": cfg.model if cfg else llm.FALLBACK_EMBED_NAME,
            "signature": sig, "dim": dim,
            "offline": cfg is None,
        },
        "kbs": kb_rows,
        "has_index": any(r["chunk_count"] for r in kb_rows),
        "running_tasks": [{"id": t.id, "kb_id": t.kb_id, "kind": t.kind, "status": t.status}
                          for t in running],
        "require_confirm": any(r["chunk_count"] for r in kb_rows),
    }


@router.post("/settings/embedding/switch")
def switch_embedding(body: schemas.EmbeddingSwitchIn, db: Session = Depends(get_db),
                     admin: models.User = Depends(require_admin)):
    cfg = db.get(models.ModelConfig, body.model_config_id)
    if not cfg or cfg.purpose != models.ModelPurpose.embedding.value:
        raise HTTPException(status_code=400, detail="请选择一个 Embedding 用途的模型配置")
    if not cfg.enabled:
        raise HTTPException(status_code=400, detail="该模型已停用")

    has_index = _has_any_index(db)
    if has_index and not body.confirm:
        raise HTTPException(status_code=409,
                            detail="已有索引，切换 Embedding 需要全量重建。请确认后重试（confirm=true）")
    if has_index:
        running = db.execute(
            select(models.Task).where(
                models.Task.status.in_([models.TaskStatus.queued.value, models.TaskStatus.running.value]),
            )
        ).scalars().first()
        if running:
            raise HTTPException(status_code=409, detail=f"有任务正在执行（#{running.id}），请等待完成")

    _clear_default(db, models.ModelPurpose.embedding.value)
    cfg.is_default = True
    cfg.enabled = True
    db.add(cfg)

    tasks = []
    if has_index:
        for kb in db.execute(select(models.KnowledgeBase)).scalars():
            chunk_count = db.execute(
                select(func.count(models.Chunk.id)).where(models.Chunk.kb_id == kb.id)
            ).scalar() or 0
            if not chunk_count:
                continue
            task = models.Task(
                kind=models.TaskKind.embedding_rebuild.value, status=models.TaskStatus.queued.value,
                stage="排队中", kb_id=kb.id, dedupe_key=f"embrebuild:{kb.id}:{cfg.id}",
                payload=json.dumps({"model_config_id": cfg.id}), created_by=admin.id,
            )
            db.add(task)
            db.flush()
            tasks.append({"kb_id": kb.id, "kb_name": kb.name, "task_id": task.id})
            kb.status = models.KbStatus.indexing.value
            kb.status_detail = "Embedding 切换中，正在全量重建"
            db.add(kb)
    audit(db, admin, "switch_embedding", target=cfg.model,
          detail=f"rebuild_kbs={len(tasks)}")
    db.commit()
    return {
        "ok": True, "model_config_id": cfg.id, "rebuild_required": has_index,
        "tasks": tasks,
        "message": ("已切换，正在为有索引的知识库全量重建" if has_index
                    else "已切换；尚无索引，无需重建"),
    }


# ------------------------------------------------------------------ 审计
@router.get("/settings/audit")
def audit_logs(page: int = 1, page_size: int = 50, db: Session = Depends(get_db),
               admin: models.User = Depends(require_admin)):
    total = db.execute(select(func.count(models.AuditLog.id))).scalar() or 0
    rows = db.execute(
        select(models.AuditLog).order_by(models.AuditLog.id.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).scalars().all()
    return {
        "items": [{
            "id": r.id, "username": r.username, "action": r.action, "target": r.target,
            "detail": r.detail, "created_at": serializers.iso(r.created_at),
        } for r in rows],
        "total": total, "page": page, "page_size": page_size,
    }
