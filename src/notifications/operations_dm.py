"""運用通知を本人指定の金沢さんのDMへ送る。DBや業務モジュールに依存しない。"""

from __future__ import annotations

import logging
import os
import re

import requests

OPERATIONS_EMAIL = "kanazawa@cnctor.jp"
_SLACK_API_BASE = "https://slack.com/api"
_REQUEST_TIMEOUT_SECONDS = 3.0


_STAGES = {"設定", "本人検索", "DM開始", "投稿", "その他"}
_REASONS = {"未設定", "HTTP拒否", "JSON不正", "Slack拒否", "タイムアウト", "接続失敗", "応答不正", "対象無効", "その他"}
_SOURCES = {
    "project_mirror", "relation_sync", "refresh_all_projects",
    "refresh_projects_incrementally", "refresh_all_client_names",
    "refresh_client_names_incrementally", "その他",
}
_ENDPOINT_STAGES = {
    "users.lookupByEmail": "本人検索", "conversations.open": "DM開始", "chat.postMessage": "投稿",
}


def _allowed(value: object, allowed: set[str]) -> str:
    # 例外属性が後から書き換わっても、外部文字列をログへ出さない。
    return value if type(value) is str and value in allowed else "その他"


class OperationsDMDeliveryError(RuntimeError):
    """工程と原因を固定語彙に限定した送信失敗。"""

    def __init__(self, stage: str, reason: str = "その他") -> None:
        self.stage = _allowed(stage, _STAGES)
        self.reason = _allowed(reason, _REASONS)
        super().__init__(safe_failure_message(self))


def safe_failure_message(error: Exception) -> str:
    stage = reason = "その他"
    if type(error) is OperationsDMDeliveryError:
        stage = _allowed(error.stage, _STAGES)
        reason = _allowed(error.reason, _REASONS)
    return f"運用DM送信失敗（工程: {stage}、原因: {reason}）"


def log_delivery_failure(
    logger: logging.Logger, error: Exception, *, source: str | None = None
) -> None:
    """例外本文や応答を出さず、固定分類だけで対処箇所を知らせる。"""
    if source is None:
        logger.warning("%s。本処理を継続します", safe_failure_message(error))
    else:
        logger.warning("%s: %s。本処理を継続します",
                       _allowed(source, _SOURCES), safe_failure_message(error))


def require_bot_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        raise OperationsDMDeliveryError("設定", "未設定")
    return token


def _api(method: str, endpoint: str, *, token: str, **kwargs) -> dict:
    stage = _ENDPOINT_STAGES.get(endpoint, "その他")
    try:
        response = requests.request(
            method, f"{_SLACK_API_BASE}/{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=_REQUEST_TIMEOUT_SECONDS, allow_redirects=False, **kwargs,
        )
        if response.status_code != 200:
            raise OperationsDMDeliveryError(stage, "HTTP拒否")
        try:
            result = response.json()
        except ValueError:
            raise OperationsDMDeliveryError(stage, "JSON不正") from None
        if not isinstance(result, dict):
            raise OperationsDMDeliveryError(stage, "JSON不正")
        if result.get("ok") is not True:
            raise OperationsDMDeliveryError(stage, "Slack拒否")
        return result
    except OperationsDMDeliveryError:
        raise
    except Exception as exc:
        if isinstance(exc, requests.Timeout):
            reason = "タイムアウト"
        elif isinstance(exc, requests.ConnectionError):
            reason = "接続失敗"
        else:
            reason = "その他"
        raise OperationsDMDeliveryError(stage, reason) from None


def send_operations_dm(text: str) -> None:
    """毎回メールで本人を解決し、DMのSlack受理を確認する。再送はしない。"""
    token = require_bot_token()
    result = _api("GET", "users.lookupByEmail", token=token,
                  params={"email": OPERATIONS_EMAIL})
    user = result.get("user")
    if not isinstance(user, dict):
        raise OperationsDMDeliveryError("本人検索", "応答不正")
    if user.get("deleted") or user.get("is_bot"):
        raise OperationsDMDeliveryError("本人検索", "対象無効")
    user_id = user.get("id")
    if not isinstance(user_id, str) or not re.fullmatch(r"[UW][A-Z0-9]+", user_id):
        raise OperationsDMDeliveryError("本人検索", "応答不正")
    result = _api("POST", "conversations.open", token=token, json={"users": user_id})
    channel = result.get("channel")
    channel_id = channel.get("id") if isinstance(channel, dict) else None
    if not isinstance(channel_id, str) or not re.fullmatch(r"D[A-Z0-9]+", channel_id):
        raise OperationsDMDeliveryError("DM開始", "応答不正")
    _api("POST", "chat.postMessage", token=token,
         json={"channel": channel_id, "text": text, "unfurl_links": False,
               "unfurl_media": False})
