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
    #
    # options="-c timezone=UTC"(shirokuma-secレビューWARN対応、2026-08-16): `EmailLog.sentAt`は
    # タイムゾーン情報を持たないPostgres `TIMESTAMP(3)`列で、UTC値として保存されている前提で
    # `reminder_check._elapsed_hours()`が読んでいる。この前提をNeon側のセッションTimeZone
    # 設定(将来変わりうる/接続経路によって異なりうる)に暗黙に依存させず、接続確立時に明示的に
    # UTCへ固定することで、経過時間計算のズレを防ぐ。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


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


def record_reminder_sent(email_log_id: str, threshold_hours: int) -> bool:
    """`(emailLogId, thresholdHours)`の記録をアトミックに試みる。新規に記録できた場合Trueを、
    既に記録済み(`ON CONFLICT DO NOTHING`で何も挿入されなかった)場合はFalseを返す。

    shirokuma-secレビューWARN対応(2026-08-16、レース条件修正): 以前は「未送信チェック→
    Slack DM送信→送信記録」の順だったため、チェックと記録の間に排他制御が無く、
    GitHub Actionsの多重起動(前回実行が長引いて次のscheduled runと重なる等)で同じ
    リマインドが2通飛びうる不整合があった。`reminder_check.run_reminder_check()`はこの
    戻り値がTrueの場合のみSlack DMを送る設計にすることで、「送信の権利」を先にDBの
    ユニーク制約(`@@unique([emailLogId, thresholdHours])`)でアトミックに獲得してから
    送るようにし、多重起動があってもDB側で自然に1回だけへ収束させる。トレードオフとして、
    記録に成功した後でSlack送信自体が失敗した場合はその回は諦める(次回の実行でも同じ
    閾値には既に記録済みのため再送されない)ことになるが、二重送信より安全な設計として
    採用する。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "EmailReminderLog" (id, "emailLogId", "thresholdHours", "sentAt")
            VALUES (%s, %s, %s, now())
            ON CONFLICT ("emailLogId", "thresholdHours") DO NOTHING
            """,
            (uuid.uuid4().hex, email_log_id, threshold_hours),
        )
        conn.commit()
        return cur.rowcount > 0
