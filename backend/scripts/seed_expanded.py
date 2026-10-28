"""导入扩充后的实验语料（三库共 55 块左右），供对比实验使用。

与 seed_demo.py 的区别：seed_demo 只导入三个原始文档（25 块），本脚本导入
demo_data 下全部语料（10 份 md，约 55 块），保证问题集有足够覆盖面。

**不再有「总量必须 ≤60 块」的约束**：Wiki 生成已改为 Map-Reduce 分片归纳，
全库每个分块都会进入某一分片（见 app/services/wiki.py 的 _plan_shards），
块数超过 60 时按字符预算切片，不再只取前 60 块。因此本脚本只校验下限
（语料太薄撑不起问题集），不设上限；要做更大规模的实验，直接往 demo_data
加文件并扩充 KBS 的 files 列表即可。

用法（在 backend 目录）：
    python -m scripts.seed_expanded              # 幂等，已导入的文档跳过
    python -m scripts.seed_expanded --reset      # 清空业务数据后重建
    python -m scripts.seed_expanded --no-worker  # 只建数据，不跑 worker
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, func, select  # noqa: E402

from app import config, models, security  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.services import chunking, parser, worker  # noqa: E402

DEMO_DIR = Path(__file__).resolve().parent.parent.parent / "demo_data"

DEMO_PASSWORD = "Demo@Passw0rd"

# 语料规划：三库合计 59 块，落在 55-60 的目标区间内。
# 每份文档按二级标题分节，一节约对应一个分块（700 字符以内不跨节合并）。
KBS = [
    {
        "name": "人事制度库",
        "description": "员工手册、培训发展、薪酬福利、考勤假期等制度文件，启用 Wiki 增强。",
        "mode": models.KbMode.wiki.value,
        "files": [
            "员工手册.md",
            "培训与职业发展制度.md",
            "薪酬与福利说明.md",
            "考勤与假期补充规定.md",
        ],
    },
    {
        "name": "客户支持库",
        "description": "支持流程、故障排查、变更发布、故障案例与客户沟通规范，基础分块模式。",
        "mode": models.KbMode.basic.value,
        "files": [
            "客户支持与运维手册.md",
            "故障案例库.md",
            "客户沟通规范.md",
        ],
    },
    {
        "name": "产品与销售库",
        "description": "产品线、功能模块、定价折扣、竞品对比、招投标与合同要点，基础分块模式。",
        "mode": models.KbMode.basic.value,
        "files": [
            "产品与销售知识.md",
            "产品功能详解.md",
            "招投标与合同要点.md",
        ],
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

# 只保留下限：低于它说明语料太薄撑不起 44 题的问题集。
# 不设上限——Wiki 已能分片覆盖全库，块数多不再是「对比不公平」的理由。
MIN_TOTAL_CHUNKS = 55


def reset(db) -> None:
    for model in (models.Message, models.Conversation, models.WikiEvidence, models.WikiSection,
                  models.WikiPageVersion, models.WikiPage, models.Chunk, models.DocumentVersion,
                  models.Document, models.Task, models.IndexVersion, models.KbGrant,
                  models.KnowledgeBase, models.AuditLog):
        db.execute(delete(model))
    db.execute(delete(models.User).where(models.User.username != config.BOOTSTRAP_ADMIN_USERNAME))
    db.commit()


def predict_chunks(path: Path) -> int:
    """用真实解析器与分块器预估块数，不写库。

    直接调 chunking.split_blocks 而不是自己数标题，保证预估与实际入库一致。
    """
    try:
        parsed = parser.parse_document(path)
        return len(chunking.split_blocks(parsed.blocks))
    except Exception as exc:  # 预检失败不阻断导入，worker 会给出真实错误
        print(f"  ! 预检失败 {path.name}：{exc.__class__.__name__}: {exc}")
        return 0


def ensure_admin(db):
    admin = db.execute(
        select(models.User).where(models.User.role == models.Role.admin.value)
    ).scalars().first()
    if admin:
        admin.must_change_password = False
        db.add(admin)
        db.commit()
        return admin
    pwd = config.BOOTSTRAP_ADMIN_PASSWORD or "Admin@Passw0rd"
    admin = models.User(
        username=config.BOOTSTRAP_ADMIN_USERNAME, display_name="系统管理员",
        password_hash=security.hash_password(pwd), role=models.Role.admin.value,
        status=models.UserStatus.active.value, must_change_password=False,
    )
    db.add(admin)
    db.commit()
    print(f"创建管理员 {admin.username} / {pwd}")
    return admin


def ensure_kbs(db, admin) -> dict[str, models.KnowledgeBase]:
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
        elif kb.mode != spec["mode"]:
            # 模式决定对比实验的分组，不允许被旧数据带偏
            kb.mode = spec["mode"]
            kb.description = spec["description"]
            db.add(kb)
            db.commit()
            print(f"校正知识库模式：{kb.name} → {spec['mode']}")
        kb_map[kb.name] = kb
    return kb_map


def ensure_users(db, admin, kb_map) -> None:
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


def enqueue_docs(db, admin, kb_map) -> int:
    """把缺失的文档入队。已存在（未删除）的同名文档直接跳过，保证幂等。"""
    queued = 0
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
                print(f"已存在，跳过：{kb.name} / {fname}")
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
            queued += 1
    return queued


def backend_is_running(host: str = "127.0.0.1", port: int = 8000) -> bool:
    """检测本机是否已有后端在跑。

    后端进程内自带 worker 线程，会与本脚本的同步 worker 抢同一批任务。
    两个 worker 同时为同一个库调 wiki.build_wiki 时，都会读到相同的
    max(version_no)，随后插入相同版本号，触发
    UNIQUE constraint failed: wiki_page_versions.page_id, version_no，
    库状态被置为 partial。这里提前拦截，而不是让用户事后排查。
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def report(db) -> tuple[int, bool]:
    """打印每库块数并校验总量，返回（总块数，是否在区间内）。"""
    print("\n各库分块统计：")
    total = 0
    for spec in KBS:
        kb = db.execute(
            select(models.KnowledgeBase).where(models.KnowledgeBase.name == spec["name"])
        ).scalar_one_or_none()
        if not kb:
            print(f"  {spec['name']}: 知识库不存在")
            continue
        db.refresh(kb)
        rows = db.execute(
            select(models.Document.filename, func.count(models.Chunk.id))
            .join(models.Chunk, models.Chunk.doc_id == models.Document.id, isouter=True)
            .where(models.Document.kb_id == kb.id, models.Document.deleted.is_(False))
            .group_by(models.Document.id)
            .order_by(models.Document.id)
        ).all()
        kb_total = 0
        for fname, cnt in rows:
            print(f"    - {fname}: {cnt} 块")
            kb_total += cnt or 0
        total += kb_total
        print(f"  {kb.name}: {kb_total} 块 / 状态 {kb.status} / {kb.status_detail}")

    ok = total >= MIN_TOTAL_CHUNKS
    print(f"\n总正文分块数：{total}（下限 {MIN_TOTAL_CHUNKS}）")
    if total < MIN_TOTAL_CHUNKS:
        print(f"⚠ 警告：总块数 {total} 低于 {MIN_TOTAL_CHUNKS}，语料偏薄，"
              f"问题集规模难以支撑")
        ok = False
    else:
        print("✓ 语料规模达标；Wiki 生成会按字符预算分片归纳，覆盖全库每个分块")
    return total, ok


def report_wiki(db) -> None:
    print("\nWiki 页面：")
    for spec in KBS:
        kb = db.execute(
            select(models.KnowledgeBase).where(models.KnowledgeBase.name == spec["name"])
        ).scalar_one_or_none()
        if not kb:
            continue
        pages = db.execute(
            select(models.WikiPage).where(models.WikiPage.kb_id == kb.id)
        ).scalars().all()
        if kb.mode != models.KbMode.wiki.value:
            print(f"  {kb.name}: 基础分块模式，不生成 Wiki")
            continue
        print(f"  {kb.name}: {len(pages)} 个页面")
        for p in pages:
            print(f"    - {p.title}（{p.status}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空业务数据后重建")
    ap.add_argument("--no-worker", action="store_true", help="不自动处理任务，仅建数据")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if not args.no_worker and backend_is_running():
            print("⚠ 检测到 127.0.0.1:8000 已有后端在运行。")
            print("  后端的进程内 worker 会与本脚本的 worker 抢同一批任务，"
                  "两个 worker 并发重建 Wiki 会撞版本号唯一约束。")
            print("  请先停掉后端再执行，或加 --no-worker 只建数据。")
            return 2

        # 预检：先按真实分块器算出总量，超上限直接提示，避免导完才发现不公平
        print("分块预检：")
        planned = 0
        for spec in KBS:
            for fname in spec["files"]:
                path = DEMO_DIR / fname
                if not path.exists():
                    continue
                n = predict_chunks(path)
                planned += n
                print(f"  {fname}: {n} 块")
        print(f"  合计：{planned} 块")
        if planned > 60:
            print(f"  提示：{planned} 块会由 Wiki 分片归纳处理"
                  f"（WIKI_MAX_MAP_CALLS 内自动压缩摘录，不会丢块）")

        if args.reset:
            reset(db)
            print("已清空业务数据")

        admin = ensure_admin(db)
        kb_map = ensure_kbs(db, admin)
        ensure_users(db, admin, kb_map)
        queued = enqueue_docs(db, admin, kb_map)
        print(f"\n本次新入队 {queued} 个文档")

        if not args.no_worker:
            print("同步执行待处理任务…")
            while worker.run_once("seed"):
                pass
            for kb in db.execute(select(models.KnowledgeBase)).scalars():
                db.refresh(kb)
                print(f"  {kb.name}: {kb.status} / {kb.status_detail}")

        total, ok = report(db)
        report_wiki(db)

        print("\n扩充语料准备完成。")
        print(f"管理员：{config.BOOTSTRAP_ADMIN_USERNAME}（密码见 WRK_ADMIN_PASSWORD）")
        print(f"员工账号密码统一：{DEMO_PASSWORD}")
        return 0 if ok else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
