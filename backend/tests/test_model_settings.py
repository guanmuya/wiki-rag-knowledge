"""模型设置与 Embedding 约束：已有索引时禁止直接换模型，必须走全量重建。"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def provider(app_client, admin_headers):
    r = app_client.post("/api/settings/providers", headers=admin_headers, json={
        "name": "测试供应商", "base_url": "http://127.0.0.1:9/v1", "api_key": "sk-fake-key-abcdefgh",
        "enabled": True,
    })
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="module")
def indexed_kb(app_client, admin_headers):
    from app.services import worker
    r = app_client.post("/api/kbs", headers=admin_headers,
                        json={"name": "Embedding 约束库", "description": "", "mode": "basic"})
    kb = r.json()
    app_client.post(f"/api/kbs/{kb['id']}/documents", headers=admin_headers,
                    files=[("files", ("x.md", "# 标题\n\n这是一段用于建立索引的测试内容，包含关键词检索。".encode(),
                                      "text/markdown"))])
    while worker.run_once("test"):
        pass
    return kb


def test_api_key_is_masked(app_client, admin_headers, provider):
    assert provider["has_key"] is True
    assert "sk-fake-key-abcdefgh" not in provider["api_key_masked"]
    listed = app_client.get("/api/settings/providers", headers=admin_headers).json()["items"]
    raw = str(listed)
    assert "sk-fake-key-abcdefgh" not in raw, "列表接口不得返回明文密钥"


def test_empty_key_update_keeps_existing(app_client, admin_headers, provider):
    r = app_client.patch(f"/api/settings/providers/{provider['id']}", headers=admin_headers,
                         json={"api_key": ""})
    assert r.status_code == 200
    assert r.json()["has_key"] is True


def test_embedding_model_change_blocked_when_indexed(app_client, admin_headers, provider, indexed_kb):
    r = app_client.post("/api/settings/models", headers=admin_headers, json={
        "provider_id": provider["id"], "purpose": "embedding", "model": "emb-a",
        "label": "向量模型 A", "is_default": True, "enabled": True,
    })
    assert r.status_code == 201, r.text
    model_a = r.json()

    # 已有索引时直接改模型标识必须被拒绝
    r2 = app_client.patch(f"/api/settings/models/{model_a['id']}", headers=admin_headers,
                          json={"model": "emb-b"})
    assert r2.status_code == 409
    assert "全量重建" in r2.json()["detail"]


def test_embedding_switch_requires_confirm(app_client, admin_headers, provider, indexed_kb):
    r = app_client.post("/api/settings/models", headers=admin_headers, json={
        "provider_id": provider["id"], "purpose": "embedding", "model": "emb-c",
        "label": "向量模型 C", "is_default": False, "enabled": True,
    })
    model_c = r.json()

    no_confirm = app_client.post("/api/settings/embedding/switch", headers=admin_headers,
                                 json={"model_config_id": model_c["id"], "confirm": False})
    assert no_confirm.status_code == 409

    ok = app_client.post("/api/settings/embedding/switch", headers=admin_headers,
                         json={"model_config_id": model_c["id"], "confirm": True})
    assert ok.status_code == 200, ok.text
    data = ok.json()
    assert data["rebuild_required"] is True
    assert data["tasks"], "切换后必须为有索引的库创建重建任务"


def test_embedding_status_reports_mismatch(app_client, admin_headers, indexed_kb):
    status = app_client.get("/api/settings/embedding/status", headers=admin_headers).json()
    assert "active" in status and "kbs" in status
    assert status["require_confirm"] is True
    entry = next(k for k in status["kbs"] if k["kb_id"] == indexed_kb["id"])
    assert entry["chunk_count"] > 0


def test_cannot_delete_embedding_model_with_index(app_client, admin_headers, provider, indexed_kb):
    models_list = app_client.get("/api/settings/models", headers=admin_headers).json()["items"]
    emb = [m for m in models_list if m["purpose"] == "embedding"]
    assert emb
    r = app_client.delete(f"/api/settings/models/{emb[0]['id']}", headers=admin_headers)
    assert r.status_code == 409


def test_connection_test_reports_failure_gracefully(app_client, admin_headers, provider):
    r = app_client.post("/api/settings/models", headers=admin_headers, json={
        "provider_id": provider["id"], "purpose": "llm", "model": "chat-test",
        "label": "对话测试", "is_default": True, "enabled": True,
    })
    mid = r.json()["id"]
    test = app_client.post(f"/api/settings/models/{mid}/test", headers=admin_headers)
    assert test.status_code == 200, test.text
    body = test.json()
    assert body["ok"] is False, "不可达地址应返回失败而不是 500"
    assert body["message"], "失败必须有可读原因"


def test_non_admin_cannot_read_settings(app_client, admin_headers):
    PWD = "EmpSet#Passw0rd5"
    created = app_client.post("/api/users", headers=admin_headers, json={
        "username": "emp_set", "display_name": "设置测试",
        "password": PWD, "role": "employee", "grants": [],
    })
    if created.status_code == 409:      # 其他用例已创建，重置为已知密码
        users = app_client.get("/api/users", headers=admin_headers).json()["items"]
        uid = next(u["id"] for u in users if u["username"] == "emp_set")
        app_client.post(f"/api/users/{uid}/reset-password", headers=admin_headers,
                        json={"new_password": PWD})
    r = app_client.post("/api/auth/login", json={"username": "emp_set", "password": PWD})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['token']}"}
    for path in ["/api/settings/providers", "/api/settings/models", "/api/settings/audit"]:
        assert app_client.get(path, headers=headers).status_code == 403, path


def test_audit_log_records_actions(app_client, admin_headers):
    logs = app_client.get("/api/settings/audit", headers=admin_headers).json()
    actions = {i["action"] for i in logs["items"]}
    assert actions, "审计日志应有内容"
    assert not any("sk-fake-key" in (i["detail"] or "") for i in logs["items"]), "日志不得包含密钥"
