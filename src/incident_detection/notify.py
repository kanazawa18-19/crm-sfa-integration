"""インシデント・アクシデント検知のSlack通知(2026-08-16)。

高優先度(スコア8点以上)の即時通知は、`User.isManager = true`のユーザー(アクセス権限用の
`role`とは別軸のフラグ、dashboard管理画面でON/OFFする想定)全員へのSlack DMで送る
(2026-08-16、コーディネーターからの追加設計変更 — 共有の運用アラートチャンネル
(`SLACK_WEBHOOK_URL_ALERT`)でも通知先メールアドレスのハードコード/env変数でもなく、
`db.find_manager_emails()`でDBから動的に解決する)。`src/meeting_sync/slack_approval.py`の
`users.lookupByEmail`→`conversations.open`→`chat.postMessage`パターンをそのまま再利用する
(`src/email_reminders/slack_notify.py`と同じ再利用方針)。使うのは既存の`SLACK_BOT_TOKEN`
(meeting_sync/email_remindersと同じ環境変数)。新規env変数は無い。

中優先度の日次ダイジェスト(`run_incident_digest()`)は既存の
金沢さんのDMへ送り、Slack受理後に送信済みを確定する。

高優先度の即時通知は、`SLACK_BOT_TOKEN`未設定・`isManager`のユーザーが0人・`find_manager_emails()`自体の失敗
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
from src.notifications import operations_dm
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
    止めないよう、対象者ごとに独立してtry/exceptする。

    `SLACK_BOT_TOKEN`未設定・managerが0人の場合も、通知をスキップした旨を`logger.warning`で
    残す(2026-08-25、`src/notifications/manager_dm.py`の`notify_managers()`と同じ対応。
    以前はログすら残さず静かにreturnしていたため、`isManager`フラグの設定漏れが起きた場合に
    「なぜ通知が届かなかったか」の痕跡が一切残らなかった)。
    """
    if not os.environ.get("SLACK_BOT_TOKEN"):
        logger.warning(
            "incident_detection: SLACK_BOT_TOKEN is not configured; skipping manager DM "
            "notification entirely (no manager will be notified) for contact_email=%s",
            contact_email,
        )
        return

    try:
        manager_emails = db.find_manager_emails()
    except Exception:
        logger.exception("incident_detection: failed to resolve manager emails")
        return
    if not manager_emails:
        logger.warning(
            "incident_detection: no managers found (User.isManager = true has 0 rows); "
            "skipping manager DM notification entirely for contact_email=%s",
            contact_email,
        )
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


class IncidentDigestDeliveryError(RuntimeError):
    """通知先URLやHTTP応答本文を含めない、日次通知の失敗。"""


def _digest_field(value: object, limit: int = 160) -> str:
    """通知の長さと改行を制限し、Slackの特殊文字をエスケープする。"""
    text = " ".join(str(value).split())
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if len(escaped) > limit:
        escaped = escaped[:limit]
        # 切断位置に掛かった文字参照だけ除く。完結した&amp;等は保持する。
        if escaped.rfind("&") > escaped.rfind(";"):
            escaped = escaped[:escaped.rfind("&")]
        escaped += "…"
    return escaped


def run_incident_digest() -> dict[str, int | bool]:
    """最大50件を通知し、Slack受理後にだけ送信済みを確定する。

    未設定・送信失敗はcronへ例外を返す。残りは次回実行に持ち越す。
    """
    try:
        operations_dm.require_bot_token()
    except operations_dm.OperationsDMDeliveryError as exc:
        raise IncidentDigestDeliveryError(operations_dm.safe_failure_message(exc)) from None

    with db.claim_undigested_medium_priority_emails() as rows:
        if not rows:
            return {"count": 0, "batch_limit_reached": False}
        lines = [f"[インシデント検知 日次ダイジェスト] 中優先度 {len(rows)}件"]
        for row in rows:
            lines.append(
                f"・{_digest_field(row['contactEmail'], 80)}"
                f"(担当: {_digest_field(row['repEmail'], 80)}) "
                f"スコア: {_digest_field(row['incidentScore'], 8)} "
                f"件名: {_digest_field(row.get('subject') or '(件名なし)', 120)}"
            )
        batch_limit_reached = len(rows) >= db.DIGEST_BATCH_SIZE
        if batch_limit_reached:
            lines.append(
                "今回の上限50件に達しました。未通知分が残っている可能性があり、次回へ持ち越します"
            )
        try:
            operations_dm.send_operations_dm("\n".join(lines))
        except operations_dm.OperationsDMDeliveryError as exc:
            raise IncidentDigestDeliveryError(operations_dm.safe_failure_message(exc)) from None

    return {"count": len(rows), "batch_limit_reached": batch_limit_reached}
