"""强制初始口令修改（P1-8）回归用例。

清单口径：`must_change_password=true` 时后端必须拒绝业务接口——前端弹窗只是体验层，
绕过前端直接打 API 也不能用初始口令办事；自助改密这条窄路径必须仍然可用，
否则用户会被锁死在门外。

这里全部走真实接口：管理员建号 → 员工登录 → 只能改密 → 改密后恢复；
外加「管理员重置口令会再次进入强制改密」和「待改密账号仍能登出」。
"""
from __future__ import annotations

import uuid

from app.deps import PASSWORD_CHANGE_ALLOWED_PATHS, path_allows_pending_password_change

INIT_PW = "Init#Passw0rd1"
NEW_PW = "Changed#Passw0rd2"
RESET_PW = "Reset#Passw0rd3"

# 业务接口代表：列表类、会话类、任务类、模型类各一条，覆盖不同的 router。
BUSINESS_PATHS = ["/api/kbs", "/api/conversations", "/api/tasks", "/api/chat/models"]


def _create_pending_user(client, admin_headers) -> str:
    username = "emp_pwd_" + uuid.uuid4().hex[:8]
    r = client.post("/api/users", headers=admin_headers, json={
        "username": username, "display_name": "强制改密", "password": INIT_PW,
        "role": "employee", "grants": [],
    })
    assert r.status_code == 201, r.text
    assert r.json()["must_change_password"] is True, "新建账号必须被要求改密"
    return username


def _login(client, username: str, password: str) -> dict:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_business_endpoints_blocked_until_password_changed(app_client, admin_headers):
    username = _create_pending_user(app_client, admin_headers)
    headers = _login(app_client, username, INIT_PW)

    # 改密流程本身需要的接口必须放行，否则用户没有出路。
    me = app_client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["user"]["must_change_password"] is True

    # 业务接口一律 403，并给出可执行的中文提示（而不是静默当匿名处理）。
    for path in BUSINESS_PATHS:
        r = app_client.get(path, headers=headers)
        assert r.status_code == 403, f"{path} 应被强制改密挡下，实际 {r.status_code}"
        assert "初始密码" in r.json()["detail"], r.json()

    ok = app_client.post("/api/auth/change-password", headers=headers,
                         json={"old_password": INIT_PW, "new_password": NEW_PW})
    assert ok.status_code == 200, ok.text

    # 改密会撤销此前签发的令牌（P1-6），用新口令重新登录后业务接口恢复。
    fresh = _login(app_client, username, NEW_PW)
    assert app_client.get("/api/auth/me", headers=fresh).json()["user"]["must_change_password"] is False
    assert app_client.get("/api/kbs", headers=fresh).status_code == 200


def test_admin_reset_puts_user_back_in_forced_change(app_client, admin_headers):
    username = _create_pending_user(app_client, admin_headers)
    first = _login(app_client, username, INIT_PW)
    app_client.post("/api/auth/change-password", headers=first,
                    json={"old_password": INIT_PW, "new_password": NEW_PW})
    normal = _login(app_client, username, NEW_PW)
    assert app_client.get("/api/kbs", headers=normal).status_code == 200

    users = app_client.get("/api/users", headers=admin_headers).json()["items"]
    uid = next(u["id"] for u in users if u["username"] == username)
    reset = app_client.post(f"/api/users/{uid}/reset-password", headers=admin_headers,
                            json={"new_password": RESET_PW})
    assert reset.status_code == 200, reset.text

    # 管理员重置口令 = 重新下发了一个初始口令，必须再走一次改密。
    again = _login(app_client, username, RESET_PW)
    blocked = app_client.get("/api/kbs", headers=again)
    assert blocked.status_code == 403, blocked.text
    assert "初始密码" in blocked.json()["detail"]
    # 重置同时撤销了重置前签发的令牌（P1-6）。
    assert app_client.get("/api/auth/me", headers=normal).status_code == 401


def test_pending_change_account_can_still_logout(app_client, admin_headers):
    """待改密账号也必须能主动登出：否则用户只能关进程。"""
    username = _create_pending_user(app_client, admin_headers)
    headers = _login(app_client, username, INIT_PW)
    assert app_client.post("/api/auth/logout", headers=headers).status_code == 200
    assert app_client.get("/api/auth/me", headers=headers).status_code == 401


def test_allowlist_covers_only_self_service_paths():
    """白名单必须窄：只放改密流程本身，业务路径一律不放。"""
    for path in PASSWORD_CHANGE_ALLOWED_PATHS:
        assert path_allows_pending_password_change(path)
        assert path_allows_pending_password_change(path + "/"), "带尾斜杠也应放行"
    for path in ["/api/kbs", "/api/users", "/api/documents/1/download",
                 "/api/settings/models", "/api/kbs/1/wiki/tree"]:
        assert not path_allows_pending_password_change(path), f"{path} 不该被放行"
