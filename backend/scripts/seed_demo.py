"""初始化演示数据：管理员、员工账号、两个不同模式的知识库、示例文档。

用法（在 backend 目录）：
    python -m scripts.seed_demo            # 幂等，可重复执行
    python -m scripts.seed_demo --reset    # 先清空业务数据再导入
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from app import config, models, security  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.services import worker  # noqa: E402

DEMO_DIR = Path(__file__).resolve().parent.parent.parent / "demo_data"

DEMO_PASSWORD = "Demo@Passw0rd"

KBS = [
    {
        "name": "人事制度库",
        "description": "员工手册、考勤、请假、报销等制度文件，启用 Wiki 增强。",
        "mode": models.KbMode.wiki.value,
        "files": ["员工手册.md"],
    },
    {
        "name": "客户支持库",
        "description": "支持流程、故障排查、变更发布与客户数据约定，基础分块模式。",
        "mode": models.KbMode.basic.value,
        "files": ["客户支持与运维手册.md"],
    },
    {
        "name": "产品与销售库",
        "description": "产品线、定价折扣、竞品对比与销售流程，基础分块模式。",
        "mode": models.KbMode.basic.value,
        "files": ["产品与销售知识.md"],
    },
]

USERS = [
    {"username": "zhangsan", "display_name": "张三（人事）", "role": "employee",
     "grants": [("人事制度库", "manage")]},
    {"username": "lisi", "display_name": "李四（客服）", "role": "employee",
     "grants": [("客户支持库", "manage"), ("人事制度库", "read")]},
    {"username": "wangwu", "display_name": "王五（销售）", "role": "employee",
     "grants": [("产品与销售库", "manage")]},
    {"username": "ops", "display_name": "运维管理员", "role": "admin", "grants": []},
]


def reset(db) -> None:
    for model in (models.Message, models.Conversation, models.WikiEvidence, models.WikiSection,
                  models.WikiPageVersion, models.WikiPage, models.Chunk, models.DocumentVersion,
                  models.Document, models.Task, models.IndexVersion, models.KbGrant,
                  models.KnowledgeBase, models.AuditLog):
        db.execute(delete(model))
    db.execute(delete(models.User).where(models.User.username != config.BOOTSTRAP_ADMIN_USERNAME))
    db.commit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true")
    ap.add_argument("--no-worker", action="store_true", help="不自动处理任务，仅建数据")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.reset:
            reset(db)
            print("已清空演示业务数据")

        admin = db.execute(
            select(models.User).where(models.User.role == models.Role.admin.value)
        ).scalars().first()
        if not admin:
            pwd = config.BOOTSTRAP_ADMIN_PASSWORD or "Admin@Passw0rd"
            admin = models.User(
                username=config.BOOTSTRAP_ADMIN_USERNAME, display_name="系统管理员",
                password_hash=security.hash_password(pwd), role=models.Role.admin.value,
                status=models.UserStatus.active.value, must_change_password=False,
            )
            db.add(admin)
            db.commit()
            print(f"创建管理员 {admin.username} / {pwd}")
        else:
            admin.must_change_password = False
            db.add(admin)
            db.commit()

        kb_map: dict[str, models.KnowledgeBase] = {}
        for spec in KBS:
            kb = db.execute(
                select(models.KnowledgeBase).where(models.KnowledgeBase.name == spec["name"])
            ).scalar_one_or_none()
            if not kb:
                kb = models.KnowledgeBase(
                    name=spec["name"], description=spec["description"], mode=spec["mode"],
                    status=models.KbStatus.empty.value, status_detail="还没有文档",
                    created_by=admin.id,
                )
                db.add(kb)
                db.commit()
                print(f"创建知识库：{kb.name}（{spec['mode']}）")
            kb_map[kb.name] = kb

        for spec in USERS:
            user = db.execute(
                select(models.User).where(models.User.username == spec["username"])
            ).scalar_one_or_none()
            if not user:
                user = models.User(
                    username=spec["username"], display_name=spec["display_name"],
                    password_hash=security.hash_password(DEMO_PASSWORD), role=spec["role"],
                    status=models.UserStatus.active.value, must_change_password=False,
                    created_by=admin.id,
                )
                db.add(user)
                db.commit()
                print(f"创建账号：{user.username} / {DEMO_PASSWORD}")
            for kb_name, level in spec["grants"]:
                kb = kb_map.get(kb_name)
                if not kb:
                    continue
                exists = db.execute(
                    select(models.KbGrant).where(
                        models.KbGrant.user_id == user.id, models.KbGrant.kb_id == kb.id)
                ).scalar_one_or_none()
                if not exists:
                    db.add(models.KbGrant(kb_id=kb.id, user_id=user.id, level=level))
                    db.commit()

        # 上传演示文档
        for spec in KBS:
            kb = kb_map[spec["name"]]
            for fname in spec["files"]:
                path = DEMO_DIR / fname
                if not path.exists():
                    print(f"跳过缺失文件：{path}")
                    continue
                exists = db.execute(
                    select(models.Document).where(
                        models.Document.kb_id == kb.id,
                        models.Document.filename == fname,
                        models.Document.deleted.is_(False),
                    )
                ).scalar_one_or_none()
                if exists:
                    continue
                doc = models.Document(
                    kb_id=kb.id, filename=fname, suffix=path.suffix, size_bytes=path.stat().st_size,
                    mime="text/markdown", status=models.KbStatus.indexing.value,
                    status_detail="等待处理", created_by=admin.id,
                )
                db.add(doc)
                db.commit()
                stored_dir = Path(config.FILES_DIR) / str(kb.id) / str(doc.id)
                stored_dir.mkdir(parents=True, exist_ok=True)
                stored = stored_dir / f"seed{doc.suffix}"
                stored.write_bytes(path.read_bytes())
                version = models.DocumentVersion(
                    doc_id=doc.id, version_no=1, storage_path=str(stored),
                    checksum=security.checksum_file(stored),
                    status=models.TaskStatus.queued.value, created_by=admin.id,
                )
                db.add(version)
                db.commit()
                task = models.Task(
                    kind=models.TaskKind.ingest_document.value,
                    status=models.TaskStatus.queued.value, stage="排队中",
                    kb_id=kb.id, doc_id=doc.id, doc_version_id=version.id,
                    dedupe_key=f"ingest:{version.id}", created_by=admin.id,
                )
                db.add(task)
                kb.status = models.KbStatus.indexing.value
                kb.status_detail = "正在处理文档"
                db.add(kb)
                db.commit()
                print(f"入队：{kb.name} / {fname}（task #{task.id}）")

        if not args.no_worker:
            print("同步执行待处理任务…")
            while worker.run_once("seed"):
                pass
            for kb in db.execute(select(models.KnowledgeBase)).scalars():
                db.refresh(kb)
                print(f"  {kb.name}: {kb.status} / {kb.status_detail}")

        print("\n演示数据准备完成。")
        print(f"管理员：{config.BOOTSTRAP_ADMIN_USERNAME}（首次密码见部署日志或 WRK_ADMIN_PASSWORD）")
        print(f"员工账号密码统一：{DEMO_PASSWORD}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
