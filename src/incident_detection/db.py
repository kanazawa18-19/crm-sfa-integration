"""EmailLogテーブル(Neon Postgres)のインシデント検知関連カラムへの直接アクセス(2026-08-16)。

`src/gmail_sync/db.py`・`src/email_reminders/db.py`と同じ方針: このDBのスキーマ管理は
dashboard(Next.js)側のPrisma(dashboard/prisma/schema.prisma)に一本化しており、ここでは
raw SQLで読み書きするのみでマイグレーションは行わない。接続文字列はdashboard側と同じ
DATABASE_URL環境変数を共有する想定。新規env変数は追加しない(SLACK_WEBHOOK_URL_ALERTは
既存を流用)。
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeout/options="-c timezone=UTC"はsrc/gmail_sync/db.py・
    # src/email_reminders/db.pyと同じ理由(ハング防止・UTC前提のタイムゾーン固定)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def update_incident_classification(email_log_id: str, score: int, priority: str | None) -> None:
    """`insert_email_log()`で先にEmailLogを記録した後、スコアリング結果のみを更新する
    経路向け(sync.py側でinsert時にまとめて渡す経路と二通りをサポートするための関数)。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'UPDATE "EmailLog" SET "incidentScore" = %s, "incidentPriority" = %s WHERE id = %s',
            (score, priority, email_log_id),
        )
        conn.commit()


def find_medium_priority_since(since: datetime) -> list[dict[str, Any]]:
    """`since`以降に記録(`createdAt`基準)された、`incidentPriority`が"medium"のEmailLogを
    全件返す(`notify.run_incident_digest()`の日次ダイジェスト向け)。`sentAt`(メール自体の
    送信日時)ではなく`createdAt`(この検知処理でEmailLogへ記録した日時)を基準にすることで、
    ダイジェスト対象が「前回実行以降に新たに検知した分」から漏れなくずれなく揃う。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, "contactEmail", "repEmail", subject, "incidentScore", "sentAt"
            FROM "EmailLog"
            WHERE "incidentPriority" = 'medium' AND "createdAt" >= %s
            ORDER BY "sentAt" DESC
            """,
            (since,),
        )
        return cur.fetchall()
