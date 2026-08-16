"""インシデント・アクシデント検知のSlack通知(2026-08-16)。

高優先度(スコア8点以上)の即時通知は、`User.isManager = true`のユーザー(アクセス権限用の
`role`とは別軸のフラグ、dashboard管理画面でON/OFFする想定)全員へのSlack DMで送る
(2026-08-16、コーディネーターからの追加設計変更 — 共有の運用アラートチャンネル
(`SLACK_WEBHOOK_URL_ALERT`)でも通知先メールアドレスのハードコード/env変数でもなく、
`db.find_manager_emails()`でDBから動的に解決する)。`src/meeting_sync/slack_approval.py`の
`users.lookupByEmail`→`conversations.open`→`chat.postMessage`パターンをそのまま再利用する
(`src/email_reminders/slack_notify.py`と同じ再利用方針)。使うのは既存の`SLACK_BOT_TOKEN`
(meeting_sync/email_remindersと同じ環境変数)。新規env変数は無い。

中優先度の日次ダイジェスト(`run_incident_digest()`)は今回変更しない(既存の
`SLACK_WEBHOOK_URL_ALERT`チャンネルのまま、合意済み)。

`SLACK_BOT_TOKEN`未設定・`isManager`のユーザーが0人・`find_manager_emails()`自体の失敗
(DB接続エラー等)のいずれの場合も何もせず静かにreturnする(`gmail_sync/notify.py`の
`notify_web_engagement_tool`と同じパターン — インシデント検知自体はメール同期処理の
副次的な効果であり、通知先解決の失敗を理由にメイン処理(EmailLog記録)を止めるべきではない)。
同じ理由で、対象者ごとのDM送信失敗も本モジュール内でtry/exceptし、1人への送信失敗が他の
対象者への送信や呼び出し元(gmail_sync.sync)へ伝播しないようにする。
"""

from __future__ import annotations

import logging
import os

import requests

from src.incident_detection import db
from src.meeting_sync.slack_approval import (
    _REQUEST_TIMEOUT_SECONDS,
    _SLACK_API_BASE,
    _resolve_dm_channel,
    _slack_headers,
)

logger = logging.getLogger(__name__)


def _send_incident_dm(manager_email: str, text: str) -> None:
    resolved = _resolve_dm_channel(manager_email)
    if resolved is None:
        raise RuntimeError(f"Slackユーザー解決に失敗しました: {manager_email}")
    channel, _user_id = resolved

    response = requests.post(
        f"{_SLACK_API_BASE}/chat.postMessage",
        headers=_slack_headers(),
        json={"channel": channel, "text": text},
        timeout=_REQUEST_TIMEOUT_SECONDS,
    )
    result = response.json()
    if not result.get("ok"):
        # Slack Web APIはHTTP 200でもエラーをbody({"ok": false, "error": ...})で返す
        # (slack_approval.py/email_reminders/slack_notify.pyと同じ注意点)。
        raise RuntimeError(f"chat.postMessage失敗: {result.get('error')}")


def notify_managers_immediate(
    *,
    subject: str | None,
    snippet: str | None,
    contact_email: str,
    rep_email: str,
    score: int,
) -> None:
    """高優先度(スコア8点以上)のインシデントを検知した際、`User.isManager = true`の各
    マネージャーへ即座にSlack DMで通知する。1人への送信失敗が他の対象者への送信を
    止めないよう、対象者ごとに独立してtry/exceptする。"""
    if not os.environ.get("SLACK_BOT_TOKEN"):
        return

    try:
        manager_emails = db.find_manager_emails()
    except Exception:
        logger.exception("incident_detection: failed to resolve manager emails")
        return
    if not manager_emails:
        return

    text = (
        "[インシデント検知 - 緊急]\n"
        f"連絡先: {contact_email}\n"
        f"担当営業: {rep_email}\n"
        f"スコア: {score}\n"
        f"件名: {subject or '(件名なし)'}"
    )
    if snippet:
        text += f"\n本文抜粋: {snippet}"

    for manager_email in manager_emails:
        try:
            _send_incident_dm(manager_email, text)
        except Exception:
            logger.exception(
                "incident_detection: failed to notify manager %s for %s", manager_email, contact_email
            )


def run_incident_digest() -> dict[str, int]:
    """`incidentPriority`が"medium"で、まだダイジェスト未送信(`digestedAt IS NULL`)の
    EmailLogをまとめて1通のSlackメッセージで送る日次ダイジェスト(`GET /api/cron/incident-digest`
    から呼ばれる想定)。0件ならSlack送信自体をスキップする。

    `db.claim_undigested_medium_priority_emails()`が対象行の`digestedAt`をアトミックに
    埋めてから返すため、以前の相対時刻ウィンドウ方式と異なりCronの実行タイミングのズレ・
    多重起動があっても二重送信/取りこぼしが起きない(shirokuma-secレビューWARN対応、
    2026-08-16、詳細は`db.claim_undigested_medium_priority_emails()`のdocstring参照)。
    """
    rows = db.claim_undigested_medium_priority_emails()
    if not rows:
        return {"count": 0}

    url = os.environ.get("SLACK_WEBHOOK_URL_ALERT")
    if not url:
        return {"count": len(rows)}

    lines = [f"[インシデント検知 日次ダイジェスト] 中優先度 {len(rows)}件"]
    for row in rows:
        lines.append(
            f"・{row['contactEmail']}(担当: {row['repEmail']}) "
            f"スコア: {row['incidentScore']} 件名: {row.get('subject') or '(件名なし)'}"
        )
    text = "\n".join(lines)

    try:
        requests.post(url, json={"text": text}, timeout=_REQUEST_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("incident_detection: failed to post digest to slack")

    return {"count": len(rows)}
