"""认证与用户管理。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, schemas, security, serializers
from ..db import get_db
from ..deps import audit, get_current_user, require_admin

router = APIRouter(tags=["auth"])


@router.post("/auth/login")
def login(body: schemas.LoginIn, db: Session = Depends(get_db)):
    user = db.execute(select(models.User).where(models.User.username == body.username)).scalar_one_or_none()
    if not user or not security.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    if user.status != models.UserStatus.active.value:
        raise HTTPException(status_code=403, detail="账号已停用，请联系管理员")
    user.last_login_at = models.utcnow()
    db.add(user)
    audit(db, user, "login", target=user.username)
    db.commit()
    token = security.issue_token(user.id, user.role)
    return {"token": token, "user": serializers.user_out(db, user)}


@router.get("/auth/me")
def me(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    grants = []
    if user.role == models.Role.admin.value:
        kbs = db.execute(select(models.KnowledgeBase).order_by(models.KnowledgeBase.id)).scalars().all()
        grants = [{"kb_id": kb.id, "level": "manage"} for kb in kbs]
    else:
        rows = db.execute(
            select(models.KbGrant).where(models.KbGrant.user_id == user.id)
        ).scalars().all()
        grants = [{"kb_id": g.kb_id, "level": g.level} for g in rows]
    return {
        "user": serializers.user_out(db, user),
        "permissions": {
            "is_admin": user.role == models.Role.admin.value,
            "kb_grants": grants,
        },
    }


@router.post("/auth/change-password")
def change_password(body: schemas.ChangePasswordIn, db: Session = Depends(get_db),
                    user: models.User = Depends(get_current_user)):
    if not security.verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    err = security.password_strength_error(body.new_password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    if security.verify_password(body.new_password, user.password_hash):
        raise HTTPException(status_code=400, detail="新密码不能与原密码相同")
    user.password_hash = security.hash_password(body.new_password)
    user.must_change_password = False
    db.add(user)
    audit(db, user, "change_password", target=user.username)
    db.commit()
    return {"ok": True}


@router.post("/auth/logout")
def logout(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)):
    audit(db, user, "logout", target=user.username)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 用户管理
@router.get("/users")
def list_users(db: Session = Depends(get_db), admin: models.User = Depends(require_admin)):
    users = db.execute(select(models.User).order_by(models.User.id)).scalars().all()
    return {"items": [serializers.user_out(db, u) for u in users], "total": len(users)}


@router.post("/users", status_code=201)
def create_user(body: schemas.UserCreateIn, db: Session = Depends(get_db),
                admin: models.User = Depends(require_admin)):
    if db.execute(select(models.User).where(models.User.username == body.username)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="用户名已存在")
    err = security.password_strength_error(body.password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    user = models.User(
        username=body.username.strip(), display_name=body.display_name.strip() or body.username,
        password_hash=security.hash_password(body.password), role=body.role,
        status=models.UserStatus.active.value, must_change_password=True, created_by=admin.id,
    )
    db.add(user)
    db.flush()
    _apply_grants(db, user, body.grants)
    audit(db, admin, "create_user", target=user.username, detail=f"role={body.role}")
    db.commit()
    return serializers.user_out(db, user)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: schemas.UserUpdateIn, db: Session = Depends(get_db),
                admin: models.User = Depends(require_admin)):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.role is not None and body.role != user.role:
        if user.role == models.Role.admin.value and body.role != models.Role.admin.value:
            _ensure_admin_remains(db, exclude_id=user.id)
        user.role = body.role
    if body.status is not None and body.status != user.status:
        if body.status != models.UserStatus.active.value:
            if user.id == admin.id:
                raise HTTPException(status_code=400, detail="不能停用自己的账号")
            if user.role == models.Role.admin.value:
                _ensure_admin_remains(db, exclude_id=user.id)
        user.status = body.status
    if body.display_name is not None:
        user.display_name = body.display_name.strip()
    db.add(user)
    audit(db, admin, "update_user", target=user.username,
          detail=f"role={user.role} status={user.status}")
    db.commit()
    return serializers.user_out(db, user)


@router.put("/users/{user_id}/grants")
def set_grants(user_id: int, body: schemas.GrantsIn, db: Session = Depends(get_db),
               admin: models.User = Depends(require_admin)):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    db.execute(models.KbGrant.__table__.delete().where(models.KbGrant.user_id == user.id))
    _apply_grants(db, user, body.grants)
    audit(db, admin, "set_grants", target=user.username,
          detail=",".join(f"{g.kb_id}:{g.level}" for g in body.grants))
    db.commit()
    return {"ok": True, "grants": serializers.user_out(db, user)["grants"]}


@router.post("/users/{user_id}/reset-password")
def reset_password(user_id: int, body: schemas.ResetPasswordIn, db: Session = Depends(get_db),
                   admin: models.User = Depends(require_admin)):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    err = security.password_strength_error(body.new_password)
    if err:
        raise HTTPException(status_code=400, detail=err)
    user.password_hash = security.hash_password(body.new_password)
    user.must_change_password = True
    db.add(user)
    audit(db, admin, "reset_password", target=user.username)
    db.commit()
    return {"ok": True}


@router.delete("/users/{user_id}")
def disable_user(user_id: int, db: Session = Depends(get_db),
                 admin: models.User = Depends(require_admin)):
    user = db.get(models.User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="不能停用自己的账号")
    if user.role == models.Role.admin.value:
        _ensure_admin_remains(db, exclude_id=user.id)
    user.status = models.UserStatus.disabled.value
    db.add(user)
    audit(db, admin, "disable_user", target=user.username)
    db.commit()
    return {"ok": True}


def _apply_grants(db: Session, user: models.User, grants: list[schemas.GrantIn]) -> None:
    seen: set[int] = set()
    for g in grants:
        if g.kb_id in seen:
            continue
        seen.add(g.kb_id)
        if not db.get(models.KnowledgeBase, g.kb_id):
            raise HTTPException(status_code=400, detail=f"知识库 {g.kb_id} 不存在")
        db.add(models.KbGrant(kb_id=g.kb_id, user_id=user.id, level=g.level))


def _ensure_admin_remains(db: Session, *, exclude_id: int) -> None:
    count = db.execute(
        select(func.count(models.User.id)).where(
            models.User.role == models.Role.admin.value,
            models.User.status == models.UserStatus.active.value,
            models.User.id != exclude_id,
        )
    ).scalar() or 0
    if count < 1:
        raise HTTPException(status_code=400, detail="系统至少需要保留一个可用管理员")
