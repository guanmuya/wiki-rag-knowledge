"""登录失败限流（P1-8）回归用例。

清单口径：登录必须有失败计数与限流。这里覆盖四件事：
1. 连续失败达到阈值后，连「密码对了」也返回 429——否则限流只是把试错变慢，
   攻击者仍能拿到真实的「密码对不对」信号（等于没有限流）；
2. 账号维度之外还有来源维度：换着用户名撞库同样会被挡；
3. 登录成功清零，合法用户不被别人的撞库拖累；
4. 锁定期过后自动解锁（进程内计数只锁一段时间，不会永久锁死）。
"""
from __future__ import annotations

import uuid

import pytest

from app import config
from app.services import login_guard

PASSWORD = "Init#Passw0rd1"


@pytest.fixture(autouse=True)
def _fresh_guard():
    """每个用例前后清空计数：锁定状态是跨用例的全局状态，不清会污染整轮测试。"""
    login_guard.reset()
    try:
        yield
    finally:
        login_guard.reset()


def _username(tag: str = "emp") -> str:
    return f"{tag}_rl_" + uuid.uuid4().hex[:8]


def _login(client, username: str, password: str):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def _create(client, admin_headers, username: str, password: str = PASSWORD) -> None:
    r = client.post("/api/users", headers=admin_headers, json={
        "username": username, "display_name": "限流测试", "password": password,
        "role": "employee", "grants": [],
    })
    assert r.status_code == 201, r.text


def test_retry_after_rounds_up_without_float_noise():
    """`now + 900 - now` 在浮点里可能是 900.0000000001，不能因此报 901。

    这条不是钻牛角尖：整轮测试里时好时坏地报 901 会让基线不稳定，
    而客户端多等一秒是实打实的体验问题。
    """
    for now in (0.1, 1234.5678, 98765.4321, 1_000_000.5, 7.0):
        assert login_guard._retry_after(now + 900, now) == 900
    # 真的还剩 0.2 秒：仍然至少报 1 秒，不能报 0。
    assert login_guard._retry_after(1000.2, 1000.0) == 1
    assert login_guard._retry_after(1000.0, 1000.0) == 1


def test_account_locks_after_threshold_and_stops_being_a_password_oracle(app_client, admin_headers, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_SOURCE", 100)
    username = _username()
    _create(app_client, admin_headers, username)

    # 前 2 次只是普通失败；第 3 次达到阈值即锁定。
    for _ in range(2):
        assert _login(app_client, username, "wrong-password").status_code == 401
    locked = _login(app_client, username, "wrong-password")
    assert locked.status_code == 429, locked.text
    assert locked.headers.get("Retry-After") == str(config.LOGIN_LOCK_SECONDS)
    assert "锁定" in locked.json()["detail"]

    # 关键断言：锁定期间即使密码正确也进不去——密码正确性不再可探测。
    oracle = _login(app_client, username, PASSWORD)
    assert oracle.status_code == 429, "锁定期间不能用密码正确与否区分响应"
    assert login_guard.status()["locked_keys"] >= 1


def test_source_dimension_blocks_username_rotation(app_client, admin_headers, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 100)          # 关掉账号维度
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_SOURCE", 4)
    # 同一个来源（TestClient 固定来源）换 4 个不存在的用户名连续失败。
    for _ in range(3):
        assert _login(app_client, _username("ghost"), "whatever").status_code == 401
    assert _login(app_client, _username("ghost"), "whatever").status_code == 429
    # 连一个真实且密码正确的账号也被这个来源挡住：说明挡的是来源而不是某一账号。
    real = _username()
    _create(app_client, admin_headers, real)
    assert _login(app_client, real, PASSWORD).status_code == 429


def test_success_resets_counter(app_client, admin_headers, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 3)
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_SOURCE", 100)
    username = _username()
    _create(app_client, admin_headers, username)

    assert _login(app_client, username, "bad-1").status_code == 401
    assert _login(app_client, username, "bad-2").status_code == 401
    assert _login(app_client, username, PASSWORD).status_code == 200     # 成功 → 清零
    # 再错两次仍不到 3 次阈值，说明计数确实被清零了。
    assert _login(app_client, username, "bad-3").status_code == 401
    assert _login(app_client, username, "bad-4").status_code == 401


def test_other_accounts_are_not_affected(app_client, admin_headers, monkeypatch):
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 2)
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_SOURCE", 100)
    victim = _username("victim")
    bystander = _username("bystander")
    _create(app_client, admin_headers, victim)
    _create(app_client, admin_headers, bystander)

    assert _login(app_client, victim, "bad").status_code == 401
    assert _login(app_client, victim, "bad").status_code == 429
    assert _login(app_client, bystander, PASSWORD).status_code == 200, "别的账号不该被牵连"


def test_lock_expires_after_window(monkeypatch):
    """用假时钟验证「锁一段时间」而不是永久锁死。"""
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 2)
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_SOURCE", 100)
    monkeypatch.setattr(config, "LOGIN_FAIL_WINDOW_SECONDS", 60)
    monkeypatch.setattr(config, "LOGIN_LOCK_SECONDS", 120)
    now = [1000.0]
    monkeypatch.setattr(login_guard, "_clock", lambda: now[0])

    user, source = "emp_fake", "10.0.0.1"
    login_guard.register_failure(user, source)
    decision = login_guard.register_failure(user, source)
    assert decision.allowed is False and decision.retry_after == 120

    now[0] += 121                        # 锁定期过去
    assert login_guard.check(user, source).allowed is True

    # 窗口也过去之后，历史失败不再计入
    now[0] += 61
    assert login_guard.status()["locked_keys"] == 0


def test_login_failures_are_audited(app_client, admin_headers, monkeypatch):
    """失败与拦截都要落审计：事后能回答「谁在撞库、锁是怎么触发的」。"""
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES", 2)
    monkeypatch.setattr(config, "LOGIN_MAX_FAILURES_PER_SOURCE", 100)
    username = _username()
    _create(app_client, admin_headers, username)
    _login(app_client, username, "bad")
    _login(app_client, username, "bad")

    items = app_client.get("/api/settings/audit", headers=admin_headers).json()["items"]
    actions = {i["action"] for i in items if i.get("target") == username}
    assert "login_failed" in actions
    assert "login_blocked" in actions
