"""首次初始化流程：桌面应用没有运维人员，管理员必须由用户自己创建。

历史问题：早期版本在后端随机生成密码并打印到日志，用户关掉弹窗就永久登录不了。
现在改为引导页创建，本文件锁定该行为。
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from app import models


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """独立的空库，用来模拟「首次安装」。

    不能用 session 级的 app_client 库——那里已有 admin，测不出初始化流程。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    url = f"sqlite:///{(tmp_path / 'fresh.sqlite3').as_posix()}"
    engine = create_engine(url, future=True)
    models.Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    yield engine, Session
    engine.dispose()


def _client(monkeypatch, engine, Session):
    """构造一个绑定到临时库的 TestClient。"""
    import importlib

    from app import db as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)

    from app import main as main_mod
    monkeypatch.setattr(main_mod, "SessionLocal", Session)

    from fastapi.testclient import TestClient
    # 不触发 lifespan，避免 worker 与 bootstrap 干扰
    client = TestClient(main_mod.app)
    return client


def test_setup_status_on_empty_db(fresh_db, monkeypatch):
    engine, Session = fresh_db
    client = _client(monkeypatch, engine, Session)
    r = client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["initialized"] is False, "空库应报告未初始化"
    assert body["org_name"] == ""


def test_meta_reports_needs_setup(fresh_db, monkeypatch):
    engine, Session = fresh_db
    client = _client(monkeypatch, engine, Session)
    meta = client.get("/api/meta").json()
    assert meta["initialized"] is False
    assert meta["needs_setup"] is True, "前端据此跳引导页，字段必须存在"


def test_create_admin_then_initialized(fresh_db, monkeypatch):
    engine, Session = fresh_db
    client = _client(monkeypatch, engine, Session)

    r = client.post("/api/setup/admin", json={
        "username": "boss", "password": "Setup#Passw0rd9",
        "display_name": "张管理", "org_name": "测试科技有限公司",
    })
    assert r.status_code == 201, r.text
    assert r.json()["username"] == "boss"

    status = client.get("/api/setup/status").json()
    assert status["initialized"] is True
    assert status["org_name"] == "测试科技有限公司"

    meta = client.get("/api/meta").json()
    assert meta["needs_setup"] is False

    # 能登录，且不强制改密（密码是用户自己设的）
    login = client.post("/api/auth/login", json={"username": "boss", "password": "Setup#Passw0rd9"})
    assert login.status_code == 200
    assert login.json()["user"]["must_change_password"] is False
    assert login.json()["user"]["role"] == "admin"


def test_setup_rejected_after_initialized(fresh_db, monkeypatch):
    engine, Session = fresh_db
    client = _client(monkeypatch, engine, Session)
    client.post("/api/setup/admin", json={"username": "boss", "password": "Setup#Passw0rd9"})

    r = client.post("/api/setup/admin", json={"username": "boss2", "password": "Another#Pass1"})
    assert r.status_code == 409, "已初始化后不得再通过该接口建管理员"
    assert "已初始化" in r.json()["detail"]

    with Session() as db:
        admins = db.execute(
            select(func.count(models.User.id)).where(models.User.role == "admin")
        ).scalar()
    assert admins == 1, "不能因为重复调用多出管理员"


def test_weak_password_rejected_with_chinese_message(fresh_db, monkeypatch):
    engine, Session = fresh_db
    client = _client(monkeypatch, engine, Session)

    r = client.post("/api/setup/admin", json={"username": "boss", "password": "weak"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert isinstance(detail, str), "必须是可读字符串，不是 FastAPI 的校验数组"
    assert "密码" in detail, f"应是中文提示，实际：{detail}"


def test_short_username_rejected(fresh_db, monkeypatch):
    engine, Session = fresh_db
    client = _client(monkeypatch, engine, Session)
    r = client.post("/api/setup/admin", json={"username": "ab", "password": "Setup#Passw0rd9"})
    assert r.status_code in (400, 422)


def test_org_name_is_optional(fresh_db, monkeypatch):
    engine, Session = fresh_db
    client = _client(monkeypatch, engine, Session)
    r = client.post("/api/setup/admin", json={"username": "boss", "password": "Setup#Passw0rd9"})
    assert r.status_code == 201
    assert client.get("/api/setup/org").json()["org_name"] == ""


def test_setup_endpoints_do_not_require_auth(fresh_db, monkeypatch):
    """初始化时用户还没登录，这些接口必须匿名可访问。"""
    engine, Session = fresh_db
    client = _client(monkeypatch, engine, Session)
    assert client.get("/api/setup/status").status_code == 200
    assert client.get("/api/setup/org").status_code == 200
    # 没有 Authorization 头也应能创建
    r = client.post("/api/setup/admin", json={"username": "boss", "password": "Setup#Passw0rd9"})
    assert r.status_code == 201
