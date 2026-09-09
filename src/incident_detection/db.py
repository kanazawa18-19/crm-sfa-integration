"""EmailLog/Userテーブル(Neon Postgres)のインシデント検知関連カラムへの直接アクセス(2026-08-16)。

`src/gmail_sync/db.py`・`src/email_reminders/db.py`と同じ方針: このDBのスキーマ管理は
dashboard(Next.js)側のPrisma(dashboard/prisma/schema.prisma)に一本化しており、ここでは
raw SQLで読み書きするのみでマイグレーションは行わない。接続文字列はdashboard側と同じ
DATABASE_URL環境変数を共有する想定。通知は既存のSLACK_BOT_TOKENでDMへ送り、
新規env変数は追加しない。

`find_manager_emails()`の実体は`src/notifications/manager_dm.py`へ移設した(2026-08-25、
`src/sync_engine/slack_notifier.py`側でも同じ「isManager=true全員へDM」要件が発生したため、
インシデント検知専用に見えるこのパッケージから、ドメイン非依存の`notifications`パッケージへ
DB解決ロジックを集約)。ここでは既存呼び出し元(`notify.py`、および
`notify.db.find_manager_emails`を直接monkeypatchしている既存テスト)との互換性のため、
薄いラッパーとして残している。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.notifications import manager_dm


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


DIGEST_BATCH_SIZE = 50


@contextmanager
def claim_undigested_medium_priority_emails() -> Iterator[list[dict[str, Any]]]:
    """送信中だけ対象行をロックし、正常終了した場合のみ送信済みにする。

    例外時は接続コンテキストがロールバックする。同時実行はロック済み行を
    飛ばす。Slack受理後の通信切断・commit失敗では再送時に重複し得る。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            '''
            SELECT id, "contactEmail", "repEmail", subject, "incidentScore", "sentAt"
            FROM "EmailLog"
            WHERE "incidentPriority" = 'medium' AND "digestedAt" IS NULL
            ORDER BY "createdAt", id
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            ''',
            (DIGEST_BATCH_SIZE,),
        )
        rows = cur.fetchall()
        yield rows
        if rows:
            cur.execute(
                'UPDATE "EmailLog" SET "digestedAt" = now() WHERE id = ANY(%s)',
                ([row["id"] for row in rows],),
            )
        conn.commit()


def find_manager_emails() -> list[str]:
    """`User.isManager = true`の全ユーザーのemailを返す(高優先度インシデント検知の即時
    Slack DM通知先、2026-08-16、コーディネーターからの追加設計変更)。

    通知先をハードコード/env変数で持つのではなく、dashboard側の管理画面でON/OFFできる
    `User.isManager`フラグ(アクセス権限用の`role`とは別軸)から動的に解決する。
    `notify.notify_managers_immediate()`がこの一覧の各emailへ個別にDM送信する。

    実体は`src.notifications.manager_dm.find_manager_emails()`(2026-08-25移設、モジュール
    docstring参照)。
    """
    return manager_dm.find_manager_emails()
