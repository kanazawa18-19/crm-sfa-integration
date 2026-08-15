"""crm-sfa-integration側で検知したメール送受信をweb-engagement-tool(MA)側へ通知する
(2026-08-16)。MA側は`POST /api/webhooks/crm-sfa-email`で受け、対応するLeadがあれば
自身のEmailLog/スコアリングパイプラインへ反映する。

既存のMA→crm-sfa-integration方向のWebhook(`WEB_ENGAGEMENT_WEBHOOK_SECRET`等)とは
逆方向・別スコープのため、専用の`WEB_ENGAGEMENT_EMAIL_WEBHOOK_SECRET`を使う。
本モジュールはあくまで副次的な連携(このメールがMA側のLeadと無関係でも同期処理自体は
継続すべき)のため、`_http.py`のHOOK_TIMEOUT_SECONDS/HOOK_MAX_RETRIES(短いタイムアウト・
少ないリトライ予算)を使う。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from src.sync_engine.clients._http import HOOK_MAX_RETRIES, HOOK_TIMEOUT_SECONDS, request_with_retry

logger = logging.getLogger(__name__)


def notify_web_engagement_tool(
    *,
    contact_email: str,
    direction: str,
    sent_at: datetime,
    subject: str | None,
    snippet: str | None,
    rep_email: str,
) -> None:
    url = os.environ.get("WEB_ENGAGEMENT_EMAIL_WEBHOOK_URL")
    secret = os.environ.get("WEB_ENGAGEMENT_EMAIL_WEBHOOK_SECRET")
    if not url or not secret:
        # 未設定でも同期処理自体は継続する(MA側連携は副次的な効果であり、
        # EmailLog記録・Notionロールアップ更新が本体)。
        return

    try:
        request_with_retry(
            "POST",
            url,
            headers={"X-Webhook-Secret": secret, "Content-Type": "application/json"},
            json_body={
                "email": contact_email,
                "direction": direction,
                "sent_at": sent_at.isoformat(),
                "subject": subject,
                "snippet": snippet,
                "rep_email": rep_email,
            },
            timeout=HOOK_TIMEOUT_SECONDS,
            max_retries=HOOK_MAX_RETRIES,
            idempotent=True,
        )
    except Exception:
        logger.exception("gmail_sync: failed to notify web-engagement-tool for %s", contact_email)
