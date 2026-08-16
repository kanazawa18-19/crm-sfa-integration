"""AppSettings/EmailLog/EmailReminderLogテーブル(Neon Postgres)への直接アクセス(2026-08-16)。

`src/gmail_sync/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで読み書きする
のみでマイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を
共有する想定。新規env変数は追加しない(SLACK_BOT_TOKEN・CRON_SECRETは既存を流用)。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeoutを明示しないとハングしうる(src/gmail_sync/db.pyと同じ理由)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10)


def get_reminder_settings() -> tuple[bool, list[int]]:
    """`AppSettings`(id=1)の`emailReminderEnabled`/`emailReminderThresholdHours`を返す。

    設定行が存在しない(通常は起きないが、dashboard側で一度も保存されていない場合)は
    無効・閾値なしとして扱う(twoFactorEnabled等と同じデフォルトOFFの設計方針)。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "emailReminderEnabled", "emailReminderThresholdHours" FROM "AppSettings" WHERE id = 1'
        )
        row = cur.fetchone()
    if row is None:
        return False, []
    return bool(row["emailReminderEnabled"]), list(row["emailReminderThresholdHours"] or [])


def find_latest_inbound_awaiting_reply() -> list[dict[str, Any]]:
    """`contactPageId`ごとの最新の`EmailLog`行のうち、`direction`が"inbound"のもの
    (＝その連絡先への直近のやり取りが受信で止まっている状態)を全件返す。

    `DISTINCT ON`で連絡先ごとの最新行を求めてから、direction="inbound"のものだけに絞る。
    新しい受信/送信メールが記録されて最新行が変われば、この結果から自然に外れる
    (古い受信メールへのリマインド判定はしない、という要件をSQL側で担保する設計)。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, "contactPageId", "contactEmail", "repEmail", subject, "sentAt"
            FROM (
                SELECT DISTINCT ON ("contactPageId")
                    id, "contactPageId", "contactEmail", "repEmail", subject, "sentAt", direction
                FROM "EmailLog"
                ORDER BY "contactPageId", "sentAt" DESC
            ) latest
            WHERE direction = 'inbound'
            """
        )
        return cur.fetchall()


def reminder_already_sent(email_log_id: str, threshold_hours: int) -> bool:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT 1 FROM "EmailReminderLog" WHERE "emailLogId" = %s AND "thresholdHours" = %s',
            (email_log_id, threshold_hours),
        )
        return cur.fetchone() is not None


def record_reminder_sent(email_log_id: str, threshold_hours: int) -> None:
    """`(emailLogId, thresholdHours)`の一意制約により、既に記録済みの組み合わせを渡すと
    例外になる(呼び出し元がreminder_already_sent()で事前に重複排除する設計、
    gmail_syncのgmailMessageId重複チェックと同じパターン)。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "EmailReminderLog" (id, "emailLogId", "thresholdHours", "sentAt")
            VALUES (%s, %s, %s, now())
            """,
            (uuid.uuid4().hex, email_log_id, threshold_hours),
        )
        conn.commit()
