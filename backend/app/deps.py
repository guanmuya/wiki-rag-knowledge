"""依赖注入：当前用户、权限校验、审计。"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, security
from .db import get_db


def _unauthorized(detail: str = "登录状态失效，请重新登录") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> models.User:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("缺少访问令牌")
    payload = security.parse_token(authorization.split(" ", 1)[1].strip())
    if not payload:
        raise _unauthorized()
    user = db.get(models.User, int(payload["uid"]))
    if not user or user.status != models.UserStatus.active.value:
        raise _unauthorized("账号不存在或已停用")
    return user


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    if user.role != models.Role.admin.value:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def grant_level(db: Session, user: models.User, kb_id: int) -> str | None:
    """返回 read / manage；管理员视为 manage。每次实时查库，撤销后旧会话立即失效。"""
    if user.role == models.Role.admin.value:
        return models.GrantLevel.manage.value
    grant = db.execute(
        select(models.KbGrant).where(
            models.KbGrant.kb_id == kb_id, models.KbGrant.user_id == user.id
        )
    ).scalar_one_or_none()
    return grant.level if grant else None


def require_kb_access(kb_id: int, *, level: str = "read"):
    """返回依赖函数：校验知识库存在 + 授权等级。"""
    def _dep(db: Session = Depends(get_db), user: models.User = Depends(get_current_user)) -> models.KnowledgeBase:
        kb = db.get(models.KnowledgeBase, kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        lv = grant_level(db, user, kb_id)
        if lv is None:
            raise HTTPException(status_code=403, detail="没有该知识库的访问权限")
        if level == "manage" and lv != models.GrantLevel.manage.value:
            raise HTTPException(status_code=403, detail="需要该知识库的管理权限")
        return kb
    return _dep


def audit(db: Session, user: models.User | None, action: str, target: str = "", detail: str = "") -> None:
    db.add(models.AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else "",
        action=action, target=target, detail=detail[:2000],
    ))
