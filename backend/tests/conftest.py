"""测试夹具：每个测试会话用独立临时数据目录，避免污染真实库。

环境变量必须在导入 app 之前设置——测试模块顶层的 `from app import models`
会在收集阶段触发 app.config 加载，因此这里在模块导入时立即生效。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

_TMP = tempfile.mkdtemp(prefix="wrk_test_")
os.environ["WRK_DATA_DIR"] = _TMP
os.environ["WRK_DB_PATH"] = str(Path(_TMP) / "test.sqlite3")
os.environ["WRK_SECRET_DIR"] = str(Path(_TMP) / "secrets")
os.environ["WRK_WORKER_ENABLED"] = "0"
# 关掉启动预热：它会起后台线程，干扰「请求路径不创建线程」这类断言。
# 预热机制本身由 test_freeze_during_task.py 单独覆盖。
os.environ["WRK_WARMUP"] = "0"
os.environ["WRK_ADMIN_USERNAME"] = "admin"
os.environ["WRK_ADMIN_PASSWORD"] = "AdminTest#2026"
os.environ["WRK_PBKDF2_ITER"] = "2000"     # 加速测试
os.environ["WRK_RRF_K"] = "60"
# 连接测试用例会连本机 9 端口；该端口若被系统 discard 服务占用，默认 120s
# 读超时会让一条用例拖住两分钟。测试环境统一压到 2s，失败语义不变。
os.environ["WRK_HTTP_TIMEOUT_READ"] = "2"

ADMIN_PASSWORD = os.environ["WRK_ADMIN_PASSWORD"]


def mark_user_ready(username: str) -> None:
    """把账号标记为「已完成初始改密」（测试辅助）。

    P1-8 之后，`must_change_password=true` 的账号会被后端拒绝业务接口——这才是
    真正的边界，前端弹窗只是体验层。用例里新建的账号（含 bootstrap 管理员）默认
    带着这个标记，而绝大多数用例考察的是业务与权限行为，不是口令策略，所以先
    完成「改密」这一步再测。

    强制改密本身由 `tests/test_must_change_password.py` 用真实接口覆盖：那条路径
    （登录 → 只放行 /auth/me、/auth/change-password、/auth/logout → 改密后恢复）
    不经过这个辅助函数。
    """
    from sqlalchemy import update

    from app import models
    from app.db import SessionLocal

    with SessionLocal() as db:
        db.execute(update(models.User)
                   .where(models.User.username == username)
                   .values(must_change_password=False))
        db.commit()


@pytest.fixture(scope="session")
def isolated_env():
    return _TMP


@pytest.fixture(scope="session")
def app_client(isolated_env):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as client:
        yield client


@pytest.fixture(scope="session")
def admin_headers(app_client):
    r = app_client.post("/api/auth/login",
                        json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    # bootstrap 出来的管理员带 must_change_password=true（真实首次登录必须改密）。
    # 这里先完成这一步，让其余用例测的是业务行为，见 mark_user_ready 的说明。
    mark_user_ready("admin")
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture()
def db_session(isolated_env):
    from app.db import SessionLocal, init_db
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def sample_text() -> str:
    return """# 差旅报销办法

## 住宿标准

一线城市住宿上限 600 元每晚，其他城市 400 元每晚。超标部分由个人承担。

## 交通标准

高铁二等座为标准配置，市内交通实报实销，单次超过 200 元需要说明事由。

## 审批流程

提交人提交单据后，依次经过直属主管、部门负责人、财务复核，最后出纳付款。
财务在收到完整单据后 5 个工作日内完成付款。

## 票据要求

所有报销必须提供合规发票，电子发票需提供 PDF 原件。超过 30 天未提交需附说明。
"""
