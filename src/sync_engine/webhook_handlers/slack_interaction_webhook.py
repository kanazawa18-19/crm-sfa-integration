"""Slack interactivity（ボタン押下コールバック）の受信ハンドラ。

`src/meeting_sync/slack_approval.py`の`post_approval_request()`が投稿した承認依頼メッセージの
「承認して登録」「対象外」ボタンが押された際、Slackが`application/x-www-form-urlencoded`で
`payload=<json>`をこのエンドポイントへPOSTしてくる（Slack API仕様）。

他のWebhookハンドラと異なり、共有トークン方式（`verify_webhook_secret`）ではなく、Slack標準の
署名検証方式（`X-Slack-Signature`/`X-Slack-Request-Timestamp`ヘッダー + 生リクエストボディを
`SLACK_SIGNING_SECRET`でHMAC-SHA256署名し比較する）で認証する。Slackは3秒以内の応答を要求する
ため、本ハンドラはNotion書き込みまで同期的に行い切ってから200を返す。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Mapping
from urllib.parse import parse_qs

from src.db_schema.registry import get_schema
from src.meeting_sync.slack_approval import handle_interaction
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.webhook_handlers._common import (
    bad_request_response,
    get_header,
    internal_error_response,
    logger,
    unauthorized_response,
)

_ACTION_DB_KEY = "action"

# Slackの署名検証タイムスタンプ許容窓（秒）。古すぎるリクエストはリプレイ攻撃とみなし拒否する
# （Slack公式ドキュメント推奨値）。
_MAX_TIMESTAMP_AGE_SECONDS = 60 * 5


def _default_action_client() -> HttpNotionClient:
    schema = get_schema(_ACTION_DB_KEY)
    return HttpNotionClient(_ACTION_DB_KEY, schema.notion_database_id)


def _verify_slack_signature(headers: Mapping[str, str], raw_body: str) -> bool:
    """`SLACK_SIGNING_SECRET`未設定時はfail-closed（常に拒否）。他のWebhookハンドラの
    fail-closed方針（`verify_webhook_secret`）と揃える。
    """
    signing_secret = os.environ.get("SLACK_SIGNING_SECRET")
    if not signing_secret:
        return False

    timestamp = get_header(headers, "X-Slack-Request-Timestamp")
    signature = get_header(headers, "X-Slack-Signature")
    if not timestamp or not signature:
        return False

    try:
        if abs(time.time() - int(timestamp)) > _MAX_TIMESTAMP_AGE_SECONDS:
            return False
    except ValueError:
        return False

    basestring = f"v0:{timestamp}:{raw_body}".encode()
    computed = "v0=" + hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)


def handler(
    event: Mapping[str, Any], context: object, *, action_client: HttpNotionClient | None = None
) -> dict[str, Any]:
    """Lambda/Cloud Functions エントリポイント（API Gateway形式のHTTPイベントを想定）。

    `action_client`未注入時は環境変数から本番用の`HttpNotionClient`を構築する
    （テスト時のみ注入を想定）。
    """
    headers = event.get("headers") or {}
    raw_body = event.get("body") or ""
    if not _verify_slack_signature(headers, raw_body):
        return unauthorized_response()

    try:
        form = parse_qs(raw_body)
        payload_values = form.get("payload")
        if not payload_values:
            raise ValueError("missing 'payload' field in form body")
        payload = json.loads(payload_values[0])
    except (ValueError, json.JSONDecodeError) as exc:
        return bad_request_response(f"invalid Slack interaction payload: {exc}")

    try:
        client = action_client if action_client is not None else _default_action_client()
        handle_interaction(payload, client)
    except Exception:
        logger.exception("unexpected error while handling Slack interaction")
        return internal_error_response()

    # Slackはボタン押下への応答として200＋空bodyを期待する（メッセージ更新は
    # response_url経由で別途行う、slack_approval.handle_interaction内で実施済み）。
    return {"statusCode": 200, "body": ""}
