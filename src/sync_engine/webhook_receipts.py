"""Webhookを「いつ・どこから」最後に受け取ったかの記録（2026-08-31）。

2026-08-31の棚卸しで `/api/webhooks/notion` の受信が3時間0件だったが、
**購読が切れているのか、たまたま変更が無かったのかを判別できなかった**。
Vercel Hobbyのランタイムログは保持が約1時間しかなく、7日分を問い合わせても
同じ結果しか返らないため、ログでは答えが出ない。

CLAUDE.md「定期実行はrc=0を信用しない」の①最終成功時刻にあたる記録をDBに持ち、
`/api/diagnostics/integrations` から読めるようにする。

**書き込みは最善努力**。ここでの失敗がWebhook本体の処理を止めてはならない
（記録できないことより、通知を取りこぼす方がはるかに有害）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

#: `record_webhook_receipt()`に渡す送信元。Webhookのパス名と揃える。
NOTION = "notion"
KINTONE = "kintone"
ZOHO = "zoho"
SPREADSHEET = "spreadsheet"


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeout/options: 他の`db.py`群と同じ理由（ハング防止・UTC固定）。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def record_webhook_receipt(source: str) -> None:
    """1件受け取ったことを記録する。失敗しても例外を投げない。"""
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO "WebhookReceipt" (source, "lastReceivedAt", "receiptCount")
                VALUES (%s, now(), 1)
                ON CONFLICT (source) DO UPDATE
                SET "lastReceivedAt" = now(),
                    "receiptCount" = "WebhookReceipt"."receiptCount" + 1
                """,
                (source,),
            )
            conn.commit()
    except Exception:  # noqa: BLE001 (記録の失敗でWebhook処理を止めない)
        logger.warning("record_webhook_receipt: 受信記録に失敗しました (source=%r)", source, exc_info=True)


def list_webhook_receipts() -> list[dict[str, Any]]:
    """全送信元の最終受信時刻と累計件数。診断エンドポイント向け。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT source, "lastReceivedAt", "receiptCount" FROM "WebhookReceipt" ORDER BY source'
        )
        return list(cur.fetchall())
