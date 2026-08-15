from __future__ import annotations

import pytest

from src.gmail_sync.token_crypto import decrypt_token, encrypt_token

_TEST_KEY = "0" * 64  # 32byte hex


def test_encrypt_decrypt_round_trip(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", _TEST_KEY)
    encrypted = encrypt_token("my-refresh-token")
    assert decrypt_token(encrypted) == "my-refresh-token"


def test_encrypt_produces_three_base64_parts(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", _TEST_KEY)
    encrypted = encrypt_token("x")
    assert len(encrypted.split(".")) == 3


def test_different_calls_produce_different_ciphertext(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", _TEST_KEY)
    first = encrypt_token("same-plaintext")
    second = encrypt_token("same-plaintext")
    assert first != second  # random IV each time


def test_missing_key_raises(monkeypatch) -> None:
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        encrypt_token("x")


def test_wrong_length_key_raises(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "too-short")
    with pytest.raises(ValueError, match="TOKEN_ENCRYPTION_KEY"):
        encrypt_token("x")


def test_tampered_ciphertext_fails_to_decrypt(monkeypatch) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", _TEST_KEY)
    encrypted = encrypt_token("secret")
    iv_b64, tag_b64, data_b64 = encrypted.split(".")
    tampered = f"{iv_b64}.{tag_b64}.{data_b64[:-4]}AAAA"
    with pytest.raises(Exception):
        decrypt_token(tampered)
