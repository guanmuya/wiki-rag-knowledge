"""密码哈希、会话令牌、供应商密钥加密。

- 密码：PBKDF2-HMAC-SHA256 加随机盐，迭代次数可调，存储格式为 algo$iter$salt$hash。
- 令牌：HMAC-SHA256 签名的无状态 token，载荷含 uid/role/exp，密钥来自独立 secret 文件。
- 供应商密钥：Fernet（AES-128-CBC + HMAC）加密，密钥文件与业务数据库分离。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config

PBKDF2_ITERATIONS = int(os.getenv("WRK_PBKDF2_ITER", "240000"))
_ALGO = "pbkdf2_sha256"


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
def _secret(name: str, nbytes: int = 48) -> bytes:
    """读取或生成密钥材料，返回 base64 urlsafe 编码后的字节串。"""
    path = Path(config.SECRET_DIR) / name
    if path.exists():
        return path.read_bytes()
    material = base64.urlsafe_b64encode(secrets.token_bytes(nbytes))
    path.write_bytes(material)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
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
    path.write_bytes(material)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
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


def issue_token(user_id: int, role: str, *, ttl_minutes: int | None = None) -> str:
    ttl = (ttl_minutes if ttl_minutes is not None else config.ACCESS_TOKEN_TTL_MINUTES) * 60
    payload = {"uid": user_id, "role": role, "exp": int(time.time()) + ttl, "n": secrets.token_hex(6)}
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
