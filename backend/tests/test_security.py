"""密码、令牌、密钥加密。"""
from __future__ import annotations

from app import security


def test_password_hash_is_salted_and_verifiable():
    h1 = security.hash_password("Str0ng#Passw0rd")
    h2 = security.hash_password("Str0ng#Passw0rd")
    assert h1 != h2, "相同密码必须产生不同哈希（随机盐）"
    assert "Str0ng#Passw0rd" not in h1, "哈希中不得出现明文"
    assert security.verify_password("Str0ng#Passw0rd", h1)
    assert security.verify_password("Str0ng#Passw0rd", h2)
    assert not security.verify_password("wrong", h1)


def test_password_strength_rules():
    assert security.password_strength_error("short") is not None
    assert security.password_strength_error("alllowercase") is not None
    assert security.password_strength_error("Str0ng#Passw0rd") is None


def test_token_roundtrip_and_tamper():
    token = security.issue_token(7, "admin", ttl_minutes=5)
    payload = security.parse_token(token)
    assert payload and payload["uid"] == 7 and payload["role"] == "admin"

    body, sig = token.split(".")
    tampered = f"{body}.{'A' * len(sig)}"
    assert security.parse_token(tampered) is None

    expired = security.issue_token(7, "admin", ttl_minutes=-1)
    assert security.parse_token(expired) is None


def test_provider_secret_encryption():
    cipher = security.encrypt_secret("sk-test-1234567890")
    assert "sk-test-1234567890" not in cipher
    assert security.decrypt_secret(cipher) == "sk-test-1234567890"
    masked = security.mask_secret(cipher)
    assert "1234567890" not in masked
    assert masked.startswith("sk-")


def test_verify_password_survives_garbage():
    assert security.verify_password("x", "not-a-valid-hash") is False
    assert security.verify_password("x", "") is False
