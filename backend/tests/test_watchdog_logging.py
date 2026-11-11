"""锁看门狗的可观测性（P1-8）回归用例。

清单口径：`services/watchdog.py` 里成片的 `except Exception: pass` 让日志轮转失败
完全静默。这里锁三件事：
1. 轮转失败必须留痕：一条 WARNING + `status()["log_rotation"]` 里的时间与原因；
2. 轮转恢复正常后状态要清掉（不能一次失败永久报警）；
3. 模块里不再有「裸 except + pass」——这是结构约束，用 AST 检查而不是肉眼看。
"""
from __future__ import annotations

import ast
import inspect
import logging
from pathlib import Path

import pytest

from app.services import watchdog


@pytest.fixture()
def watchdog_log(tmp_path, monkeypatch):
    """把看门狗的日志路径与体积上限换到临时目录，避免碰真实 DATA_DIR。"""
    path = tmp_path / "lock-watchdog.log"
    monkeypatch.setattr(watchdog, "_LOG_PATH", path)
    monkeypatch.setattr(watchdog, "_MAX_LOG_BYTES", 64)
    watchdog._rotation_state.clear()
    try:
        yield path
    finally:
        watchdog._rotation_state.clear()


def test_rotation_success_moves_file_and_keeps_state_clean(watchdog_log, caplog):
    watchdog_log.write_bytes(b"x" * 200)
    with caplog.at_level(logging.WARNING, logger="wrk.watchdog"):
        watchdog._rotate_if_needed()

    backup = watchdog_log.with_suffix(".log.1")
    assert backup.exists(), "超过上限的日志应被滚到 .1"
    assert not watchdog_log.exists()
    status = watchdog.status()["log_rotation"]
    assert status["failed"] is False and status["error"] == ""
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], "正常轮转不该告警"


def test_rotation_failure_warns_and_is_visible_in_status(watchdog_log, caplog):
    # 用「backup 路径上有个目录」制造真实失败：unlink/rename 都做不了。
    backup = watchdog_log.with_suffix(".log.1")
    backup.mkdir()
    watchdog_log.write_bytes(b"x" * 200)

    with caplog.at_level(logging.WARNING, logger="wrk.watchdog"):
        watchdog._rotate_if_needed()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and r.name == "wrk.watchdog"]
    assert warnings, "日志轮转失败必须有告警，不能静默"
    assert "轮转失败" in warnings[0].getMessage()

    rotation = watchdog.status()["log_rotation"]
    assert rotation["failed"] is True and rotation["at"]
    assert rotation["error"], "诊断状态里要能查到失败原因"
    assert watchdog_log.exists(), "轮转失败时不能把原日志弄丢"


def test_rotation_recovers_and_clears_failure_state(watchdog_log, caplog):
    backup = watchdog_log.with_suffix(".log.1")
    backup.mkdir()
    watchdog_log.write_bytes(b"x" * 200)
    watchdog._rotate_if_needed()
    assert watchdog.status()["log_rotation"]["failed"] is True

    backup.rmdir()                     # 占用解除
    with caplog.at_level(logging.INFO, logger="wrk.watchdog"):
        watchdog._rotate_if_needed()

    assert watchdog.status()["log_rotation"]["failed"] is False
    assert backup.exists()


def test_diagnostics_exposes_p18_observability(app_client, admin_headers):
    """诊断接口要能一眼看到 P1-8 的三处状态，不必让用户翻日志目录。"""
    body = app_client.get("/api/diagnostics/process", headers=admin_headers).json()

    rotation = body["watchdog"]["log_rotation"]
    assert rotation["failed"] is False and rotation["max_bytes"] > 0
    assert {"account_threshold", "source_threshold", "lock_seconds"} <= set(body["login_guard"])
    assert body["secrets"]["enabled"] is True
    assert body["secrets"]["platform"] in {"windows-acl", "posix-mode"}


def test_no_silent_except_remains_in_watchdog():
    """结构约束：模块内不允许出现 `except ...: pass`。

    看门狗必须自吞异常，但「自吞」不等于「不留痕」——每个入口都要经过
    `_ignore()`（debug 级）或明确的 warning。这条用例防止后续改动又把
    `pass` 加回来。
    """
    tree = ast.parse(inspect.getsource(watchdog))
    silent: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        body = [n for n in node.body if not isinstance(n, ast.Expr)
                or not isinstance(getattr(n, "value", None), ast.Constant)]
        if len(body) == 1 and isinstance(body[0], ast.Pass):
            silent.append(node.lineno)
    assert silent == [], f"这些 except 分支仍然是静默的：行 {silent}"
