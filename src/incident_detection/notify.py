"""インシデント・アクシデント検知のSlack通知(2026-08-16)。

`src/sync_engine/slack_notifier.py`(WebhookSlackNotifier)と同じ、`SLACK_WEBHOOK_URL_ALERT`
環境変数のIncoming WebhookへシンプルにHTTP POSTする方式(`src/sync_engine/slack_notifier.py`・
`src/meeting_sync/slack_approval.py`が使っているのと同じ既存の運用アラート用Webhook)。
新規env変数は無い。

未設定時は何もせず静かにreturnする(`gmail_sync/notify.py`の`notify_web_engagement_tool`と
同じパターン — インシデント検知自体はメール同期処理の副次的な効果であり、Webhook未設定を
理由にメイン処理(EmailLog記録)を止めるべきではない)。同じ理由で、Slack送信自体の失敗も
本モジュール内でtry/exceptして呼び出し元(gmail_sync.sync)へ伝播させない。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

from src.incident_detection import db

logger = logging.getLogger(__name__)

_DIGEST_WINDOW = timedelta(hours=24)
_REQUEST_TIMEOUT_SECONDS = 10


def notify_managers_immediate(
    *,
    subject: str | None,
    snippet: str | None,
    contact_email: str,
    rep_email: str,
    score: int,
) -> None:
    """高優先度(スコア8点以上)のインシデントを検知した際、即座にマネージャー陣へ通知する。"""
    url = os.environ.get("SLACK_WEBHOOK_URL_ALERT")
    if not url:
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

    try:
        requests.post(url, json={"text": text}, timeout=_REQUEST_TIMEOUT_SECONDS)
    except Exception:
        logger.exception("incident_detection: failed to notify managers for %s", contact_email)


def run_incident_digest() -> dict[str, int]:
    """直近24時間で`incidentPriority`が"medium"のEmailLogをまとめて1通のSlackメッセージで
    送る日次ダイジェスト(`GET /api/cron/incident-digest`から呼ばれる想定)。0件ならSlack送信
    自体をスキップする。"""
    since = datetime.now(timezone.utc) - _DIGEST_WINDOW
    rows = db.find_medium_priority_since(since)
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
