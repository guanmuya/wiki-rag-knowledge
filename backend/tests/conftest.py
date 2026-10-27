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
os.environ["WRK_ADMIN_USERNAME"] = "admin"
os.environ["WRK_ADMIN_PASSWORD"] = "AdminTest#2026"
os.environ["WRK_PBKDF2_ITER"] = "2000"     # 加速测试
os.environ["WRK_RRF_K"] = "60"

ADMIN_PASSWORD = os.environ["WRK_ADMIN_PASSWORD"]


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
