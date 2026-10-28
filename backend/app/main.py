"""应用入口：初始化数据库、引导管理员、启动 Worker。"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select

from . import config, models, security
from .db import SessionLocal, init_db
from .routers import auth, chat, kbs, settings, setup, wiki
from .services import worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("wrk")

APP_NAME = "企业知识库文档整理与问答系统"
VERSION = "1.0.1"


def bootstrap_admin() -> None:
    """首次部署时准备管理员。

    行为分两种，取决于是否显式配置了初始密码：

    1. 配置了 `WRK_ADMIN_PASSWORD`（开发/测试/自动化部署）：直接建号。
       这样脚本、CI、seed 流程可以无人值守跑起来。

    2. 没配置（真实用户首次安装）：**不建号**，等用户在前端引导页自己设。
       早期版本在这里随机生成密码并打印到日志，结果用户关掉弹窗就永久登录不了——
       桌面应用没有运维人员，这个设计是错的。

    已有管理员时永远直接返回，不会覆盖。
    """
    with SessionLocal() as db:
        exists = db.execute(
            select(func.count(models.User.id)).where(models.User.role == models.Role.admin.value)
        ).scalar() or 0
        if exists:
            return
        if not config.BOOTSTRAP_ADMIN_PASSWORD:
            log.info("尚未初始化管理员，等待用户通过引导页完成初始化")
            return
        admin = models.User(
            username=config.BOOTSTRAP_ADMIN_USERNAME,
            display_name="系统管理员",
            password_hash=security.hash_password(config.BOOTSTRAP_ADMIN_PASSWORD),
            role=models.Role.admin.value,
            status=models.UserStatus.active.value,
            must_change_password=True,
        )
        db.add(admin)
        db.commit()
        banner = (
            "\n" + "=" * 62 +
            f"\n首次部署：已创建管理员账号\n  用户名：{admin.username}"
            "\n  密码来自 WRK_ADMIN_PASSWORD 环境变量"
            "\n  首次登录后必须修改密码\n" + "=" * 62
        )
        log.warning(banner)
        print(banner, flush=True)


def needs_setup() -> bool:
    """是否需要走首次初始化引导。空库（无任何管理员）时为真。"""
    with SessionLocal() as db:
        admins = db.execute(
            select(func.count(models.User.id)).where(models.User.role == models.Role.admin.value)
        ).scalar() or 0
        return admins == 0


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    bootstrap_admin()
    worker.start()
    log.info("%s v%s 启动完成", APP_NAME, VERSION)
    try:
        yield
    finally:
        worker.stop()


app = FastAPI(title=APP_NAME, version=VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router, prefix="/api")
app.include_router(setup.router, prefix="/api")
app.include_router(kbs.router, prefix="/api")
app.include_router(wiki.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(settings.router, prefix="/api")


@app.get("/api/meta")
def meta():
    with SessionLocal() as db:
        admins = db.execute(
            select(func.count(models.User.id)).where(models.User.role == models.Role.admin.value)
        ).scalar() or 0
        initialized = admins > 0
        has_llm = db.execute(
            select(models.ModelConfig.id).where(
                models.ModelConfig.purpose == models.ModelPurpose.llm.value,
                models.ModelConfig.enabled.is_(True)).limit(1)
        ).first()
        has_embed = db.execute(
            select(models.ModelConfig.id).where(
                models.ModelConfig.purpose == models.ModelPurpose.embedding.value,
                models.ModelConfig.enabled.is_(True)).limit(1)
        ).first()
        org = db.get(models.AppSetting, "org_name")
    return {
        "app_name": APP_NAME, "version": VERSION, "initialized": initialized,
        # 前端据此决定是否跳首次初始化引导页
        "needs_setup": not initialized,
        "org_name": org.value if org else "",
        "offline_models": not (has_llm and has_embed),
        "capabilities": {
            "llm_configured": bool(has_llm),
            "embedding_configured": bool(has_embed),
            "worker_enabled": config.WORKER_ENABLED,
        },
    }


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION}
