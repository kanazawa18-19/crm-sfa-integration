"""見積書承認リクエストの結果(承認/却下/取消)を依頼者(営業担当者)へSlack DM通知する(2026-08-18)。

`src/email_reminders/slack_notify.py`と同じ、`src/meeting_sync/slack_approval.py`の
「メールアドレス→DM解決」パターン(`_resolve_dm_channel`)をそのまま再利用する。
"""

from __future__ import annotations

import logging
import os

import requests

from src.meeting_sync.slack_approval import (
    _REQUEST_TIMEOUT_SECONDS,
    _SLACK_API_BASE,
    _resolve_dm_channel,
    _slack_headers,
)

logger = logging.getLogger(__name__)

_STATUS_LABELS_JA = {"approved": "承認", "declined": "却下", "cancelled": "取消"}


def notify_quote_approval_result(
    *, requested_by_email: str, project_name: str, approver_email: str, status: str
) -> None:
    """通知失敗時も例外を送出しない(ポーリングcronは他の承認リクエストの処理を続けたいため。
    ステータス自体は呼び出し元で既にDBへ反映済みであり、この通知はベストエフォート)。"""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        logger.warning("SLACK_BOT_TOKEN is not set; skip quote approval result notification")
        return

    try:
        resolved = _resolve_dm_channel(requested_by_email)
    except Exception:
        logger.warning(
            "Slack user resolution raised an exception for %r; skip quote approval result "
            "notification",
            requested_by_email,
            exc_info=True,
        )
        return
    if resolved is None:
        logger.warning(
            "Slack user resolution failed for %r; skip quote approval result notification",
            requested_by_email,
        )
        return
    channel, _user_id = resolved

    label = _STATUS_LABELS_JA.get(status, status)
    text = (
        f"*見積書の承認リクエストが{label}されました*\n"
        f"案件: {project_name}\n"
        f"承認者: {approver_email}"
    )
    try:
        response = requests.post(
            f"{_SLACK_API_BASE}/chat.postMessage",
            headers=_slack_headers(),
            json={"channel": channel, "text": text},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        result = response.json()
        if not result.get("ok"):
            logger.warning("Slack chat.postMessage failed: %s", result.get("error"))
    except Exception:
        logger.warning("failed to send quote approval result Slack DM", exc_info=True)
