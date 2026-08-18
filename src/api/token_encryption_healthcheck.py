"""`TOKEN_ENCRYPTION_KEY`環境変数の自己診断(2026-08-18)。

2026-08-16〜08-18の2日間、`TOKEN_ENCRYPTION_KEY`が不正な値になっていたため、Gmail連携
(`src/gmail_sync/`)の同期が全件サイレントに失敗し続けていた。`sync_all()`は担当者ごとに
try/exceptで独立させる設計のため、Vercel Cron自体は毎日200 OKを返し続け、Vercelの
Runtime Errorsダッシュボード(誰も定常的に見ていない)以外に失敗が一切表面化しなかった。

このモジュールは`encrypt_token`→`decrypt_token`のラウンドトリップを毎日実行し、失敗時は
既存の運用アラートSlackチャンネル(`SLACK_WEBHOOK_URL_ALERT`、`src/incident_detection/notify.py`
の日次ダイジェストと同じ宛先)へ即座に通知する。Gmail連携・Drive連携(見積書承認フロー)は
いずれも同じ`decrypt_token`/`encrypt_token`(`src/gmail_sync/token_crypto.py`)を共有している
ため、この1つの自己診断で両方の障害を検知できる。
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import requests

from src.gmail_sync.token_crypto import decrypt_token, encrypt_token

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 10


def check_token_encryption_key() -> dict[str, Any]:
    """`encrypt_token`→`decrypt_token`のラウンドトリップを検証する。

    往復が一致しない、あるいは例外が送出された場合は`ok: False`を返す(呼び出し元が
    Slack通知するかどうかを判断する。ここでは通知しない)。
    """
    test_value = f"healthcheck-{uuid.uuid4().hex}"
    try:
        decrypted = decrypt_token(encrypt_token(test_value))
    except Exception as exc:  # noqa: BLE001 - 診断対象の例外種別を問わず捕捉する
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    if decrypted != test_value:
        return {"ok": False, "error": "round-trip mismatch"}
    return {"ok": True, "error": None}


def _notify_slack_alert(message: str) -> None:
    url = os.environ.get("SLACK_WEBHOOK_URL_ALERT")
    if not url:
        return
    try:
        requests.post(url, json={"text": message}, timeout=_REQUEST_TIMEOUT_SECONDS)
    except Exception:
        # アラート送信自体の失敗でヘルスチェックのレスポンスを壊さない
        # (incident_detection/notify.pyの日次ダイジェストと同じ方針)。
        logger.exception("token_encryption_healthcheck: failed to post alert to slack")


def run_token_encryption_healthcheck() -> dict[str, Any]:
    result = check_token_encryption_key()
    if not result["ok"]:
        logger.error("token_encryption_healthcheck failed: %s", result["error"])
        _notify_slack_alert(
            "[緊急] TOKEN_ENCRYPTION_KEYの自己診断に失敗しました。"
            "Gmail連携・Drive連携(見積書承認フロー)が機能不全の可能性があります。"
            f"詳細: {result['error']}"
        )
    return result
