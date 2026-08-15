"""dashboard(Next.js)側のlib/tokenCrypto.tsと同じAES-256-GCM実装(2026-08-16)。

暗号文フォーマット: `base64(12byte IV).base64(16byte authTag).base64(ciphertext)`
(ドット区切り3パート)。同じTOKEN_ENCRYPTION_KEY環境変数を共有することで、
dashboard側で暗号化したGmail OAuthリフレッシュトークンをこちらで復号できる
(逆方向も可)。

NodeのcreateCipheriv("aes-256-gcm", ...)はauthTagをciphertextと別に返すが、
PythonのAESGCM.encrypt()は「ciphertext + tag」を1つのbytesとして返す仕様のため、
末尾16byteをtagとして分離している。
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_TAG_LENGTH_BYTES = 16
_IV_LENGTH_BYTES = 12


def _get_key() -> bytes:
    hex_key = os.environ.get("TOKEN_ENCRYPTION_KEY")
    if not hex_key or len(hex_key) != 64:
        raise ValueError(
            "TOKEN_ENCRYPTION_KEY must be a 32-byte hex string (generate with: openssl rand -hex 32)"
        )
    return bytes.fromhex(hex_key)


def encrypt_token(plaintext: str) -> str:
    key = _get_key()
    iv = os.urandom(_IV_LENGTH_BYTES)
    ciphertext_with_tag = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    ciphertext, tag = ciphertext_with_tag[:-_TAG_LENGTH_BYTES], ciphertext_with_tag[-_TAG_LENGTH_BYTES:]
    return ".".join(base64.b64encode(part).decode("ascii") for part in (iv, tag, ciphertext))


def decrypt_token(payload: str) -> str:
    iv_b64, tag_b64, data_b64 = payload.split(".")
    iv = base64.b64decode(iv_b64)
    tag = base64.b64decode(tag_b64)
    data = base64.b64decode(data_b64)
    key = _get_key()
    plaintext = AESGCM(key).decrypt(iv, data + tag, None)
    return plaintext.decode("utf-8")
