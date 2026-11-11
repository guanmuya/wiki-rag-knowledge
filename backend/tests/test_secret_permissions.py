"""密钥文件/目录权限收紧（P1-8）回归用例。

清单口径：密钥文件写入后 `os.chmod(0o600)` 在 Windows 上不设 ACL，等于什么也没做。
用例不依赖真实平台（CI 是 Linux、本机是 Windows），两条分支都用替换点验证：
- Windows：断言 icacls 参数（去继承 + 只授权当前账号 / Administrators / SYSTEM），
  并断言失败只告警不抛异常、状态里能查到原因、修好之后会重试；
- POSIX：断言文件 0600、目录 0700；
- 另外验证 `_secret()` 写完密钥确实走同一条收紧路径，以及关掉开关时完全不动。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app import config, security


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    security.reset_hardening_state()
    monkeypatch.setattr(security, "_HARDEN_DISABLED", False)
    yield
    security.reset_hardening_state()


def _recorder(calls: list, *, returncode: int = 0, output: str = ""):
    """假的 subprocess.run：记录参数，必要时把「icacls 输出」写进传入的文件。"""
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        sink = kwargs.get("stdout")
        if output and sink is not None:
            sink.write(output)
        return SimpleNamespace(returncode=returncode)
    return fake_run


# ---------------------------------------------------------------- Windows 分支
def test_windows_acl_removes_inheritance_and_grants_minimum(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "CORP")
    calls: list = []
    monkeypatch.setattr(security, "_RUNNER", _recorder(calls))

    target = tmp_path / "token.key"
    target.write_bytes(b"x")
    assert security.harden_path(target, is_dir=False) is True

    argv, kwargs = calls[0]
    assert argv[0] == "icacls", "必须走系统自带的 icacls，不引入新依赖"
    assert str(target) in argv
    assert "/inheritance:r" in argv, "不去掉继承，同一台机器的其他账号仍然可读"
    assert "CORP\\alice:F" in argv, "当前账号必须保有读写权限"
    assert "*S-1-5-32-544:F" in argv, "Administrators 组（SID 与系统语言无关）"
    assert "*S-1-5-18:F" in argv, "SYSTEM"
    assert "shell" not in kwargs, "不允许走 shell 拼接"
    assert kwargs.get("timeout"), "外部命令必须有超时，不能卡住启动"
    # 输出走临时文件而不是管道：Windows 上 Popen 会给每个管道起读线程，
    # 而本项目的硬约束是「请求路径不要创建线程」（DESIGN.md §4.5）。
    assert "capture_output" not in kwargs, "不能用管道收输出（会创建读线程）"
    assert hasattr(kwargs.get("stdout"), "write"), "输出应写入临时文件"


def test_windows_directory_uses_inheritable_rights(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "CORP")
    calls: list = []
    monkeypatch.setattr(security, "_RUNNER", _recorder(calls))

    assert security.harden_path(tmp_path, is_dir=True) is True
    argv = calls[0][0]
    assert "CORP\\alice:(OI)(CI)F" in argv, "目录必须让新建文件继承权限"
    assert "/inheritance:r" in argv


def test_windows_failure_is_reported_not_raised(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "_WINDOWS", True)

    def boom(*args, **kwargs):
        raise OSError("拒绝访问")

    monkeypatch.setattr(security, "_RUNNER", boom)
    target = tmp_path / "token.key"
    assert security.harden_path(target, is_dir=False) is False, "失败不应抛异常"
    failed = security.hardening_status()["failed"]
    assert failed, "失败原因必须能被诊断接口看到"
    assert "拒绝访问" in next(iter(failed.values()))

    # 失败不缓存：权限被修好之后下一次写入会重试成功
    calls: list = []
    monkeypatch.setattr(security, "_RUNNER", _recorder(calls))
    assert security.harden_path(target, is_dir=False) is True
    assert security.hardening_status()["failed"] == {}


def test_windows_nonzero_exit_code_counts_as_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "_WINDOWS", True)
    monkeypatch.setattr(security, "_RUNNER",
                        _recorder([], returncode=5, output="系统找不到指定的文件。"))
    assert security.harden_path(tmp_path / "t.key", is_dir=False) is False
    assert "系统找不到指定的文件" in next(iter(security.hardening_status()["failed"].values()))


# ---------------------------------------------------------------- POSIX 分支
def test_posix_file_is_0600_and_dir_is_0700(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "_WINDOWS", False)
    seen: list[tuple[str, int]] = []
    real_chmod = os.chmod

    def fake_chmod(path, mode, *args, **kwargs):
        seen.append((str(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(security.os, "chmod", fake_chmod)
    target = tmp_path / "token.key"
    target.write_bytes(b"x")
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()

    assert security.harden_path(target, is_dir=False) is True
    assert security.harden_path(secrets_dir, is_dir=True) is True
    assert (str(target), 0o600) in seen
    assert (str(secrets_dir), 0o700) in seen
    assert security.hardening_status()["platform"] == "posix-mode"


# ---------------------------------------------------------------- 接入点与开关
def test_secret_write_hardens_dir_and_file(monkeypatch, tmp_path):
    """写密钥即收紧：目录与文件各收紧一次，不是只在别处调一下。"""
    secret_dir = tmp_path / "secrets"
    monkeypatch.setattr(config, "SECRET_DIR", secret_dir)
    monkeypatch.setattr(security, "_WINDOWS", True)
    monkeypatch.setenv("USERNAME", "alice")
    monkeypatch.setenv("USERDOMAIN", "CORP")
    calls: list = []
    monkeypatch.setattr(security, "_RUNNER", _recorder(calls))

    material = security._secret("unit-test.key")
    assert material and (secret_dir / "unit-test.key").exists()

    targets = [argv[1] for argv, _ in calls]
    assert str(secret_dir) in targets, "目录本身也要收紧"
    assert str(secret_dir / "unit-test.key") in targets, "密钥文件要收紧"
    assert security.hardening_status()["hardened_count"] >= 2


def test_kill_switch_skips_hardening(monkeypatch, tmp_path):
    monkeypatch.setattr(security, "_HARDEN_DISABLED", True)
    monkeypatch.setattr(security, "_RUNNER",
                        lambda *a, **k: pytest.fail("关掉开关时不该调用外部命令"))
    assert security.harden_path(tmp_path / "x.key", is_dir=False) is True
    assert security.hardening_status()["enabled"] is False
