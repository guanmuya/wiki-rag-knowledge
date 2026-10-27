"""权限回归测试：越权、撤销授权、管理员保底。"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def kb(app_client, admin_headers):
    r = app_client.post("/api/kbs", headers=admin_headers,
                        json={"name": "权限测试库", "description": "", "mode": "basic"})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="module")
def employee(app_client, admin_headers, kb):
    r = app_client.post("/api/users", headers=admin_headers, json={
        "username": "emp_perm", "display_name": "权限测试员工",
        "password": "Emp#Passw0rd9", "role": "employee",
        "grants": [{"kb_id": kb["id"], "level": "read"}],
    })
    assert r.status_code == 201, r.text
    return r.json()


def _login(app_client, username, password):
    r = app_client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_login_rejects_wrong_password(app_client):
    r = app_client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_unauthenticated_access_blocked(app_client):
    for path in ["/api/kbs", "/api/users", "/api/conversations", "/api/tasks"]:
        assert app_client.get(path).status_code == 401, path


def test_employee_cannot_see_other_kbs(app_client, admin_headers, employee, kb):
    r = app_client.post("/api/kbs", headers=admin_headers,
                        json={"name": "员工不可见库", "description": "", "mode": "basic"})
    hidden = r.json()
    headers = _login(app_client, "emp_perm", "Emp#Passw0rd9")
    items = app_client.get("/api/kbs", headers=headers).json()["items"]
    ids = {i["id"] for i in items}
    assert kb["id"] in ids
    assert hidden["id"] not in ids, "未授权知识库不应出现在列表里"
    assert app_client.get(f"/api/kbs/{hidden['id']}", headers=headers).status_code == 403


def test_employee_cannot_manage_users(app_client, employee):
    headers = _login(app_client, "emp_perm", "Emp#Passw0rd9")
    assert app_client.get("/api/users", headers=headers).status_code == 403
    assert app_client.post("/api/kbs", headers=headers,
                           json={"name": "x", "description": "", "mode": "basic"}).status_code == 403


def test_employee_with_read_cannot_upload(app_client, employee, kb):
    headers = _login(app_client, "emp_perm", "Emp#Passw0rd9")
    r = app_client.post(f"/api/kbs/{kb['id']}/documents", headers=headers,
                        files={"files": ("a.md", b"# hi\ncontent here", "text/markdown")})
    assert r.status_code == 403


def test_revoke_grant_invalidates_existing_conversation(app_client, admin_headers, employee, kb):
    headers = _login(app_client, "emp_perm", "Emp#Passw0rd9")
    conv = app_client.post("/api/conversations", headers=headers,
                           json={"title": "授权测试", "kb_ids": [kb["id"]]})
    assert conv.status_code == 201, conv.text
    cid = conv.json()["id"]

    # 撤销授权
    r = app_client.put(f"/api/users/{employee['id']}/grants", headers=admin_headers,
                       json={"grants": []})
    assert r.status_code == 200

    # 旧会话继续提问必须被拒绝
    ask = app_client.post(f"/api/conversations/{cid}/ask", headers=headers,
                          json={"question": "住宿标准是多少"})
    assert ask.status_code == 403, "撤销授权后旧会话必须重新校验并拒绝"

    # 会话详情应标记该库不可访问
    detail = app_client.get(f"/api/conversations/{cid}", headers=headers).json()
    assert detail["kb_access"][0]["accessible"] is False

    # 恢复授权供后续用例使用
    app_client.put(f"/api/users/{employee['id']}/grants", headers=admin_headers,
                   json={"grants": [{"kb_id": kb["id"], "level": "read"}]})


def test_must_keep_at_least_one_admin(app_client, admin_headers):
    me = app_client.get("/api/auth/me", headers=admin_headers).json()
    admin_id = me["user"]["id"]
    r = app_client.patch(f"/api/users/{admin_id}", headers=admin_headers,
                         json={"status": "disabled"})
    assert r.status_code == 400
    r2 = app_client.patch(f"/api/users/{admin_id}", headers=admin_headers,
                          json={"role": "employee"})
    assert r2.status_code == 400


def test_new_user_must_change_password(app_client, admin_headers):
    r = app_client.post("/api/users", headers=admin_headers, json={
        "username": "emp_chpwd", "display_name": "改密测试",
        "password": "Init#Passw0rd1", "role": "employee", "grants": [],
    })
    assert r.status_code == 201
    assert r.json()["must_change_password"] is True

    headers = _login(app_client, "emp_chpwd", "Init#Passw0rd1")
    bad = app_client.post("/api/auth/change-password", headers=headers,
                          json={"old_password": "Init#Passw0rd1", "new_password": "weak"})
    assert bad.status_code == 400
    ok = app_client.post("/api/auth/change-password", headers=headers,
                         json={"old_password": "Init#Passw0rd1", "new_password": "Changed#2026x"})
    assert ok.status_code == 200
    assert app_client.get("/api/auth/me", headers=headers).json()["user"]["must_change_password"] is False


def test_duplicate_username_rejected(app_client, admin_headers):
    r = app_client.post("/api/users", headers=admin_headers, json={
        "username": "emp_perm", "display_name": "重复",
        "password": "Dup#Passw0rd11", "role": "employee", "grants": [],
    })
    assert r.status_code == 409


def test_download_requires_permission(app_client, admin_headers, kb):
    """没有授权时下载必须 403，而不是返回文件。"""
    app_client.post("/api/users", headers=admin_headers, json={
        "username": "emp_nodl", "display_name": "下载测试",
        "password": "NoDl#Passw0rd7", "role": "employee", "grants": [],
    })
    headers = _login(app_client, "emp_nodl", "NoDl#Passw0rd7")
    r = app_client.get("/api/documents/1/download", headers=headers)
    assert r.status_code in (403, 404)
