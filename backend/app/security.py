"""密码哈希、会话令牌、供应商密钥加密。

- 密码：PBKDF2-HMAC-SHA256 加随机盐，迭代次数可调，存储格式为 algo$iter$salt$hash。
- 令牌：HMAC-SHA256 签名的 token，载荷含 uid/role/exp/ver/n，密钥来自独立 secret 文件。
  ver 是签发时用户的令牌版本，鉴权时与 users.token_version 比对，不等即作废（P1-6）。
  n 是每次签发的随机 nonce，仅保证令牌唯一、不参与撤销判定。
- 供应商密钥：Fernet（AES-128-CBC + HMAC）加密，密钥文件与业务数据库分离。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger("wrk.security")

PBKDF2_ITERATIONS = int(os.getenv("WRK_PBKDF2_ITER", "240000"))
_ALGO = "pbkdf2_sha256"


# ---------------------------------------------------------------- 密钥文件权限（P1-8）
# 密钥文件（token.key / provider.key）泄露 = 令牌可伪造 + 供应商 API Key 可解密。
# 旧实现在写完文件后调 `os.chmod(path, 0o600)`：POSIX 上正确，**Windows 上等于没做**
# ——chmod 在 Windows 只改只读位、不设置 ACL，同一台机器上的其他账号照样能读。
# 所以这里按平台分开：
# - POSIX：文件 0600、目录 0700；
# - Windows：用系统自带的 icacls 去掉 ACL 继承，只授权「当前账号 + Administrators +
#   SYSTEM」。不写成「只授当前账号」是刻意的：管理员与系统进程要能做备份/扫描，
#   而把 Administrators 排除掉会让「换个账号运行桌面版」直接读不到密钥。
# 失败只记告警、不抛异常：读不到密钥（应用起不来、供应商 Key 全部失效）比权限没
# 收紧更糟。失败原因留在 hardening_status() 里，诊断接口能看到。
_HARDEN_DISABLED = not config.HARDEN_SECRET_PERMISSIONS
_WINDOWS = os.name == "nt"
_RUNNER = subprocess.run          # 测试替换点：不真的调 icacls

# Administrators / SYSTEM 的内置 SID：与系统语言无关，比组名可靠。
_ADMIN_SIDS = ("*S-1-5-32-544", "*S-1-5-18")

_hardened: set[str] = set()
_errors: dict[str, str] = {}


def _harden_key(path: Path | str, *, is_dir: bool) -> str:
    return f"{'dir' if is_dir else 'file'}:{path}"


def _windows_principal() -> str:
    """当前进程身份，形如 `DOMAIN\\user`（icacls 的授权对象）。"""
    user = os.environ.get("USERNAME") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    return f"{domain}\\{user}" if domain and user else user


def _harden_argv(path: Path | str, *, is_dir: bool) -> list[str]:
    """拼 icacls 命令行。拆成纯函数就是为了能直接断言参数，不必真跑系统命令。"""
    rights = ":(OI)(CI)F" if is_dir else ":F"
    argv = ["icacls", str(path), "/inheritance:r"]
    principal = _windows_principal()
    for who in ((principal,) if principal else ()) + _ADMIN_SIDS:
        argv += ["/grant:r", f"{who}{rights}"]
    return argv + ["/c", "/q"]


def _run_icacls(argv: list[str]) -> tuple[int, str]:
    """执行 icacls，返回 (退出码, 输出文本)。

    刻意**不用管道**（`capture_output=True`）：Windows 上 Popen 会给每个管道起一条
    读线程，而本项目有一条硬约束——请求路径不要创建线程（DESIGN.md §4.5，
    `tests/test_freeze_during_task.py` 守着）。改用临时文件收输出，零额外线程。
    """
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as out:
        proc = _RUNNER(argv, stdout=out, stderr=out, timeout=15)
        out.seek(0)
        return getattr(proc, "returncode", 0), out.read().strip()


def harden_path(path: Path | str, *, is_dir: bool) -> bool:
    """收紧密钥文件/目录权限；成功返回 True，失败只记告警并返回 False。"""
    if _HARDEN_DISABLED:
        return True
    target = Path(path)
    key = _harden_key(target, is_dir=is_dir)
    if key in _hardened:
        return True
    try:
        if _WINDOWS:
            code, detail = _run_icacls(_harden_argv(target, is_dir=is_dir))
            if code != 0:
                raise OSError(detail[:300] or f"icacls 退出码 {code}")
        else:
            os.chmod(target, 0o700 if is_dir else 0o600)
    except Exception as exc:      # noqa: BLE001 - 权限收紧失败绝不能挡住密钥使用
        _errors[key] = f"{type(exc).__name__}: {exc}"[:300]
        log.warning("收紧密钥%s权限失败（%s）：%s",
                    "目录" if is_dir else "文件", target, exc)
        return False
    _hardened.add(key)
    _errors.pop(key, None)
    return True


def harden_secret_dir() -> None:
    """收紧密钥目录本身：目录不收紧的话，文件权限再对也能被列目录/替换。"""
    try:
        config.SECRET_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:        # pragma: no cover - 目录不可建
        log.warning("无法准备密钥目录（%s）：%s", config.SECRET_DIR, exc)
        return
    harden_path(config.SECRET_DIR, is_dir=True)


def hardening_status() -> dict:
    """给诊断接口用的只读状态：平台、是否启用、成功数量、失败原因。"""
    return {
        "enabled": not _HARDEN_DISABLED,
        "platform": "windows-acl" if _WINDOWS else "posix-mode",
        "hardened_count": len(_hardened),
        "failed": dict(_errors),
    }


def reset_hardening_state() -> None:
    """清空「已收紧」缓存。测试用；运维手工修复权限后也可调用来重试。"""
    _hardened.clear()
    _errors.clear()


# ---------------------------------------------------------------- 密码
def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != _ALGO:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


def password_strength_error(password: str) -> str | None:
    if len(password) < 10:
        return "密码至少 10 位"
    kinds = sum([
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ])
    if kinds < 3:
        return "密码需包含大写、小写、数字、符号中的至少三类"
    return None


# ---------------------------------------------------------------- 令牌
def _write_secret(path: Path, material: bytes) -> None:
    """密钥落盘即收紧（P1-8）：先收紧目录（顺带补建），再收紧文件。

    顺序有讲究：目录还没收紧时写文件，等于把明文密钥先放进一个谁都能列的目录里；
    而 `harden_secret_dir()` 里带 mkdir，运行期目录被删掉也能自愈。
    """
    harden_secret_dir()
    path.write_bytes(material)
    harden_path(path, is_dir=False)


def _secret(name: str, nbytes: int = 48) -> bytes:
    """读取或生成密钥材料，返回 base64 urlsafe 编码后的字节串。"""
    path = Path(config.SECRET_DIR) / name
    if path.exists():
        return path.read_bytes()
    material = base64.urlsafe_b64encode(secrets.token_bytes(nbytes))
    _write_secret(path, material)
    return material


def _fernet_key() -> bytes:
    """Fernet 要求恰好 32 字节原始密钥，对应 44 字节 base64 文本。"""
    path = Path(config.SECRET_DIR) / "provider.key"
    if path.exists():
        raw = path.read_bytes()
        try:
            if len(base64.urlsafe_b64decode(raw)) == 32:
                return raw
        except Exception:
            pass
        # 旧版本生成的密钥长度不符，重新生成（已加密数据需重新填写 API Key）
        path.unlink(missing_ok=True)
    material = base64.urlsafe_b64encode(secrets.token_bytes(32))
    _write_secret(path, material)
    return material


_TOKEN_KEY = None


def token_key() -> bytes:
    global _TOKEN_KEY
    if _TOKEN_KEY is None:
        _TOKEN_KEY = _secret("token.key")
    return _TOKEN_KEY


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def issue_token(user_id: int, role: str, *, token_version: int = 0,
                ttl_minutes: int | None = None) -> str:
    """签发令牌：把签发时用户的令牌版本记进 ver，供 deps.get_current_user 校验撤销。

    默认 0 是为了兼容脚本 / 测试里直接调用 issue_token(uid, role) 的旧写法；
    真正的登录路径必须传入用户当前的 token_version。
    """
    ttl = (ttl_minutes if ttl_minutes is not None else config.ACCESS_TOKEN_TTL_MINUTES) * 60
    payload = {
        "uid": user_id, "role": role, "exp": int(time.time()) + ttl,
        "ver": int(token_version), "n": secrets.token_hex(6),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64e(hmac.new(token_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def parse_token(token: str) -> dict | None:
    try:
        body, sig = token.split(".")
        expected = _b64e(hmac.new(token_key(), body.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(_b64d(body))
        if int(payload.get("exp", 0)) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


# ---------------------------------------------------------------- 供应商密钥
_FERNET = None


def _fernet() -> Fernet:
    global _FERNET
    if _FERNET is None:
        _FERNET = Fernet(_fernet_key())
    return _FERNET


def encrypt_secret(plain: str) -> str:
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode()


def decrypt_secret(cipher: str) -> str:
    if not cipher:
        return ""
    try:
        return _fernet().decrypt(cipher.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def mask_secret(plain_or_cipher: str, *, already_cipher: bool = True) -> str:
    value = decrypt_secret(plain_or_cipher) if already_cipher else plain_or_cipher
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}****{value[-4:]}"


def checksum_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()
