"""見積書承認リクエストの結果(承認/却下/取消)を依頼者(営業担当者)へSlack DM通知する(2026-08-18)。

`src/email_reminders/slack_notify.py`と同じ、`src/meeting_sync/slack_approval.py`の
「メールアドレス→DM解決」パターン(`_resolve_dm_channel`)をそのまま再利用する。

複数承認者対応(2026-08-27): 承認者一覧は`、`区切りの1行ではなく箇条書き(改行＋`・`)にする
(人数が増えると読みにくいため、obasan-qualityレビューWARN対応)。また却下(`declined`)時は
「誰が却下したか」を`approval_state`(`GoogleDriveDocClient.get_approval()`の生レスポンス)の
`reviewerResponses[]`から特定できれば明示する。ただし**このフィールドが実レスポンスで
返ってくることは未検証**(公式リファレンス上の存在は確認済み、`get_approval()`は
`fields=*`を指定しているため取得できる可能性が高いが2026-08-27時点で実機未確認)なため、
`_extract_declined_reviewers()`は形が想定と異なる場合も例外を投げず空リストを返し、
呼び出し元は従来どおり承認者全員の列挙にフォールバックする(docs/quote_approval_note.md参照)。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

from src.meeting_sync.slack_approval import (
    _REQUEST_TIMEOUT_SECONDS,
    _SLACK_API_BASE,
    _resolve_dm_channel,
    _slack_headers,
)

logger = logging.getLogger(__name__)

_STATUS_LABELS_JA = {"approved": "承認", "declined": "却下", "cancelled": "取消"}


def _extract_declined_reviewers(approval_state: dict[str, Any]) -> list[str]:
    """`approval_state`(`GoogleDriveDocClient.get_approval()`の生レスポンス)の
    `reviewerResponses[]`から、`response == "DECLINED"`を返したreviewerのメールアドレスを
    特定する(未検証のためベストエフォート、モジュールdocstring参照)。フィールドが無い・
    要素の形が想定と異なる場合は例外を送出せず空リストを返す。"""
    try:
        responses = approval_state.get("reviewerResponses") or []
        declined: list[str] = []
        for entry in responses:
            if not isinstance(entry, dict) or entry.get("response") != "DECLINED":
                continue
            reviewer = entry.get("reviewer")
            if isinstance(reviewer, str):
                declined.append(reviewer)
            elif isinstance(reviewer, dict):
                email = reviewer.get("emailAddress") or reviewer.get("email")
                if email:
                    declined.append(email)
        return declined
    except Exception:
        logger.warning(
            "failed to parse reviewerResponses from Drive approval_state; falling back to "
            "listing all approvers",
            exc_info=True,
        )
        return []


def notify_quote_approval_result(
    *,
    requested_by_email: str,
    project_name: str,
    approver_emails: list[str],
    status: str,
    approval_state: dict[str, Any] | None = None,
) -> None:
    """通知失敗時も例外を送出しない(ポーリングcronは他の承認リクエストの処理を続けたいため。
    ステータス自体は呼び出し元で既にDBへ反映済みであり、この通知はベストエフォート)。

    `approval_state`は却下(`status == "declined"`)時にのみ、却下者特定のために使う
    (未検証のフォールバック付き、`_extract_declined_reviewers()`参照)。省略時・特定できない
    場合は承認者全員を列挙する従来の文面になる。
    """
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
    declined_by = _extract_declined_reviewers(approval_state) if status == "declined" and approval_state else []
    lines = [f"*見積書の承認リクエストが{label}されました*", f"案件: {project_name}"]
    if declined_by:
        lines.append(f"却下した承認者: {'、'.join(declined_by)}")
    lines.append("承認者:")
    lines.extend(f"・{email}" for email in approver_emails)
    text = "\n".join(lines)
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
