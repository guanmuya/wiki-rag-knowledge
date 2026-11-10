"""令牌可撤销（P1-6）回归用例。

清单口径：改密、管理员重置密码、登出都会让用户此前签发的令牌立即失效，
旧 token 调任意需鉴权接口返回 401；用新口令重新登录拿到的令牌仍然可用。
三项行为各有一条用例（清单「三条均有测试」）。
"""
from __future__ import annotations

from app import security

OLD_PW = "Old#Passw0rd2026"
NEW_PW = "New#Passw0rd2027"


def _create_user(client, admin_headers, username: str, password: str) -> int:
    r = client.post("/api/users", headers=admin_headers, json={
        "username": username, "display_name": username,
        "password": password, "role": "employee", "grants": [],
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _login(client, username: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_issued_token_carries_current_version():
    """签发的令牌把用户当时版本写进载荷，供鉴权时比对（撤销机制的载体）。"""
    assert security.parse_token(
        security.issue_token(7, "admin", token_version=3, ttl_minutes=5))["ver"] == 3
    assert security.parse_token(
        security.issue_token(7, "admin", ttl_minutes=5))["ver"] == 0


def test_change_password_revokes_old_token(app_client, admin_headers):
    """改密后旧 token 调任意需鉴权接口都返回 401，重新登录的新 token 可用。"""
    _create_user(app_client, admin_headers, "revoke_changepw", OLD_PW)
    token = _login(app_client, "revoke_changepw", OLD_PW)
    assert app_client.get("/api/auth/me", headers=_auth(token)).status_code == 200

    r = app_client.post("/api/auth/change-password", headers=_auth(token),
                        json={"old_password": OLD_PW, "new_password": NEW_PW})
    assert r.status_code == 200, r.text

    # 认证接口与业务接口共用 get_current_user，旧 token 都应被拒。
    assert app_client.get("/api/auth/me", headers=_auth(token)).status_code == 401
    assert app_client.get("/api/kbs", headers=_auth(token)).status_code == 401

    fresh = _login(app_client, "revoke_changepw", NEW_PW)
    assert app_client.get("/api/auth/me", headers=_auth(fresh)).status_code == 200


def test_reset_password_revokes_old_token(app_client, admin_headers):
    """管理员重置口令后，该用户此前签发的 token 立即失效。"""
    uid = _create_user(app_client, admin_headers, "revoke_resetpw", OLD_PW)
    token = _login(app_client, "revoke_resetpw", OLD_PW)
    assert app_client.get("/api/auth/me", headers=_auth(token)).status_code == 200

    r = app_client.post(f"/api/users/{uid}/reset-password", headers=admin_headers,
                        json={"new_password": NEW_PW})
    assert r.status_code == 200, r.text

    assert app_client.get("/api/auth/me", headers=_auth(token)).status_code == 401
    assert app_client.get("/api/kbs", headers=_auth(token)).status_code == 401

    fresh = _login(app_client, "revoke_resetpw", NEW_PW)
    assert app_client.get("/api/auth/me", headers=_auth(fresh)).status_code == 200


def test_logout_revokes_old_token(app_client, admin_headers):
    """登出后，前端丢弃的那枚 token 不再被服务端接受。"""
    _create_user(app_client, admin_headers, "revoke_logout", OLD_PW)
    token = _login(app_client, "revoke_logout", OLD_PW)
    assert app_client.get("/api/auth/me", headers=_auth(token)).status_code == 200

    r = app_client.post("/api/auth/logout", headers=_auth(token))
    assert r.status_code == 200, r.text

    assert app_client.get("/api/auth/me", headers=_auth(token)).status_code == 401
    assert app_client.get("/api/kbs", headers=_auth(token)).status_code == 401
