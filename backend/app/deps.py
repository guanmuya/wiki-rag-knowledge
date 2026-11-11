"""依赖注入：当前用户、权限校验、审计。"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, security
from .db import get_db

# 强制初始口令修改（P1-8）：`must_change_password` 以前只写库、只给前端看，
# 后端从不校验，于是绕过前端（脚本 / 直接打 API）就能用初始口令调用全部业务
# 接口——管理员下发的初始口令等于长期有效。真正的边界必须在这里：待改密的
# 账号只放行「自助改密」这条窄路径，其余一律 403。
#
# 路径清单刻意保持最短，且都是改密流程本身需要的：
# - /auth/me              前端登录后要知道自己被要求改密，也用来确认改密结果
# - /auth/change-password 改密动作本身
# - /auth/logout          允许放弃这次会话（否则用户只能关进程）
PASSWORD_CHANGE_ALLOWED_PATHS = (
    "/api/auth/me",
    "/api/auth/change-password",
    "/api/auth/logout",
)


def _unauthorized(detail: str = "登录状态失效，请重新登录") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail,
                         headers={"WWW-Authenticate": "Bearer"})


def path_allows_pending_password_change(path: str) -> bool:
    """待改密账号能否访问这个路径。拆成函数是为了能单独测、也便于将来加白名单。"""
    normalized = (path or "").rstrip("/")
    return any(normalized == allowed or normalized.endswith(allowed)
               for allowed in PASSWORD_CHANGE_ALLOWED_PATHS)


def get_current_user(
    request: Request,
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
    # 令牌可撤销（P1-6）：载荷里的版本必须等于用户当前版本。改密、管理员重置
    # 密码、登出都会递增 token_version，使此前签发的全部令牌立即失效。
    # 老库补列前签发的令牌没有 ver，按版本 0 处理；一旦发生过撤销（版本 > 0）
    # 同样会被这里挡下。
    if int(payload.get("ver", 0)) != int(user.token_version or 0):
        raise _unauthorized()
    # 强制初始口令修改（P1-8）。放在 401 之后：先确认「这个令牌还有效」，
    # 再谈「这个有效会话能不能办事」，两种失败对前端是不同的处理路径。
    if user.must_change_password and not path_allows_pending_password_change(request.url.path):
        raise HTTPException(
            status_code=403,
            detail="请先修改初始密码，再使用其他功能",
        )
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
