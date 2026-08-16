"""EmailLogテーブル(Neon Postgres)のインシデント検知関連カラムへの直接アクセス(2026-08-16)。

`src/gmail_sync/db.py`・`src/email_reminders/db.py`と同じ方針: このDBのスキーマ管理は
dashboard(Next.js)側のPrisma(dashboard/prisma/schema.prisma)に一本化しており、ここでは
raw SQLで読み書きするのみでマイグレーションは行わない。接続文字列はdashboard側と同じ
DATABASE_URL環境変数を共有する想定。新規env変数は追加しない(SLACK_WEBHOOK_URL_ALERTは
既存を流用)。
"""

from __future__ import annotations

import os
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


def claim_undigested_medium_priority_emails() -> list[dict[str, Any]]:
    """`incidentPriority`が"medium"で、まだ日次ダイジェストに載せていない
    (`digestedAt IS NULL`)EmailLogを、アトミックに"claim"(`digestedAt`をnow()で埋める)
    してから返す(`notify.run_incident_digest()`向け、shirokuma-secレビューWARN対応、
    2026-08-16)。

    以前は素朴な相対時刻ウィンドウ(`createdAt >= now() - 24h`)で対象を絞っていたが、
    Vercel Cronの実行タイミングのズレ・多重起動があると同じインシデントが2日連続で
    ダイジェストに載ったり、逆に一生載らず漏れたりする問題があった。
    `src/email_reminders/db.py`の`record_reminder_sent()`と同じ「送信の権利を先にDB側で
    アトミックに獲得してから送る」設計を踏襲し、`UPDATE ... WHERE "digestedAt" IS NULL
    ... RETURNING`の単一クエリで「対象の特定」と「claim」を同時に行う(別クエリに分けると
    そこにも別のレース条件が生まれるため、`record_reminder_sent()`より一歩進めて1クエリに
    まとめている)。

    トレードオフとして、claim(`digestedAt`更新・コミット)に成功した後でSlack送信自体が
    失敗した場合、その回のダイジェストからは漏れる(`email_reminders.reminder_check`と
    同じ設計判断 — 二重送信より安全な設計として採用する)。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            UPDATE "EmailLog"
            SET "digestedAt" = now()
            WHERE "incidentPriority" = 'medium' AND "digestedAt" IS NULL
            RETURNING id, "contactEmail", "repEmail", subject, "incidentScore", "sentAt"
            """
        )
        rows = cur.fetchall()
        conn.commit()
    return rows
