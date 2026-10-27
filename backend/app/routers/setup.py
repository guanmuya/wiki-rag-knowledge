"""首次初始化：桌面应用没有运维人员，管理员必须由用户自己创建。

设计要点：
1. `GET /setup/status` 无需登录，前端据此决定是否跳引导页。
2. `POST /setup/admin` **只在没有任何管理员时可调用**，用条件写入防并发重复创建。
   一旦初始化完成，这个接口永久返回 409，不能用来添加管理员
   （那要走管理员登录后的 `POST /users`）。
3. 企业名称存在 `app_settings`，用于侧边栏标题；可选。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import models, security
from ..db import get_db
from ..deps import audit

router = APIRouter(tags=["setup"])

SETTING_ORG_NAME = "org_name"


class SetupStatusOut(BaseModel):
    initialized: bool
    org_name: str = ""
    app_name: str = ""
    version: str = ""


class SetupAdminIn(BaseModel):
    # 这里放宽长度校验，交给 security.password_strength_error 给出中文原因。
    # 用 Field(min_length=10) 会让 FastAPI 返回英文 422，与全站中文错误风格不一致。
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = ""
    org_name: str = Field(default="", max_length=64)


@router.get("/setup/status")
def setup_status(db: Session = Depends(get_db)):
    from ..main import APP_NAME, VERSION
    admins = db.execute(
        select(func.count(models.User.id)).where(models.User.role == models.Role.admin.value)
    ).scalar() or 0
    org = db.get(models.AppSetting, SETTING_ORG_NAME)
    return {
        "initialized": admins > 0,
        "org_name": org.value if org else "",
        "app_name": APP_NAME,
        "version": VERSION,
    }


@router.post("/setup/admin", status_code=201)
def setup_admin(body: SetupAdminIn, db: Session = Depends(get_db)):
    # 条件检查与写入在同一个事务里，避免两个并发请求都建成管理员
    admins = db.execute(
        select(func.count(models.User.id)).where(models.User.role == models.Role.admin.value)
    ).scalar() or 0
    if admins > 0:
        raise HTTPException(status_code=409, detail="系统已初始化，请直接登录")

    username = body.username.strip()
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="用户名至少 3 个字符")
    if db.execute(select(models.User).where(models.User.username == username)).scalar_one_or_none():
        raise HTTPException(status_code=409, detail="用户名已存在")

    err = security.password_strength_error(body.password)
    if err:
        raise HTTPException(status_code=400, detail=err)

    admin = models.User(
        username=username,
        display_name=body.display_name.strip() or "系统管理员",
        password_hash=security.hash_password(body.password),
        role=models.Role.admin.value,
        status=models.UserStatus.active.value,
        # 用户自己设的密码，不需要再强制改一次
        must_change_password=False,
    )
    db.add(admin)
    db.flush()

    org_name = body.org_name.strip()
    if org_name:
        setting = db.get(models.AppSetting, SETTING_ORG_NAME)
        if setting:
            setting.value = org_name
        else:
            db.add(models.AppSetting(key=SETTING_ORG_NAME, value=org_name))

    audit(db, admin, "setup_admin", target=admin.username, detail=f"org={org_name}")
    db.commit()
    return {"ok": True, "user_id": admin.id, "username": admin.username}


@router.get("/setup/org")
def get_org(db: Session = Depends(get_db)):
    """侧边栏标题用。无需登录——登录页也要显示企业名。"""
    setting = db.get(models.AppSetting, SETTING_ORG_NAME)
    return {"org_name": setting.value if setting else ""}
