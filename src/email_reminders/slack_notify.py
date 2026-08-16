"""未返信メールリマインドのSlack DM送信(2026-08-16)。

`src/meeting_sync/slack_approval.py`の`users.lookupByEmail`→`conversations.open`→
`chat.postMessage`という「メールアドレスから担当者を解決してDM送信」パターンをそのまま
再利用する(`_resolve_dm_channel`等の内部ヘルパーをimportして使う。承認ボタン等の状態は
持たない単純な一方向通知のため、専用モジュールとして薄く実装する)。

使うのは既存の`SLACK_BOT_TOKEN`(meeting_syncと同じ環境変数)。新規env変数は無い。
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


class SlackDeliveryError(RuntimeError):
    """DM送信に失敗した場合に送出する。呼び出し元(reminder_check.run_reminder_check())が
    対象ごとにtry/exceptで独立させ、1件の送信失敗が他の対象への通知を止めないようにする。"""


def send_reminder_dm(rep_email: str, contact_email: str, hours_elapsed: int, subject: str | None) -> None:
    """`rep_email`へ、`contact_email`からの未返信メールについてのリマインドDMを送る。

    送信に失敗した場合(Slackユーザー解決失敗・chat.postMessage失敗)は`SlackDeliveryError`
    を送出する。`SLACK_BOT_TOKEN`未設定時も同様に送出する(post_approval_request()とは異なり
    黙ってFalseを返さない — リマインド機能自体は`emailReminderEnabled`で明示的にON/OFFされる
    設計のため、ONなのに送信手段が無い状態を黙って無視すると誰も気づけないため)。
    """
    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        raise SlackDeliveryError("SLACK_BOT_TOKEN is not set")

    resolved = _resolve_dm_channel(rep_email)
    if resolved is None:
        raise SlackDeliveryError(f"Slackユーザー解決に失敗しました: {rep_email}")
    channel, _user_id = resolved

    subject_line = f"件名: {subject}\n" if subject else ""
    text = (
        f"*未返信メールのリマインド*\n"
        f"{contact_email} からのメールにまだ返信していません（受信から約{hours_elapsed}時間経過）。\n"
        f"{subject_line}"
    )
    try:
        response = requests.post(
            f"{_SLACK_API_BASE}/chat.postMessage",
            headers=_slack_headers(),
            json={"channel": channel, "text": text},
            timeout=_REQUEST_TIMEOUT_SECONDS,
        )
        result = response.json()
    except Exception as exc:
        raise SlackDeliveryError(f"chat.postMessage呼び出し中に例外発生: {exc}") from exc

    if not result.get("ok"):
        # Slack Web APIはHTTP 200でもエラーをbody({"ok": false, "error": ...})で返す
        # (slack_approval.pyのpost_approval_request()と同じ注意点)。
        logger.warning("Slack chat.postMessage failed: %s", result.get("error"))
        raise SlackDeliveryError(f"chat.postMessage失敗: {result.get('error')}")
