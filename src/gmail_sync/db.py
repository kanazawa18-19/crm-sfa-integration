"""RepGmailConnection/EmailLogテーブル(Neon Postgres)への直接アクセス(2026-08-16)。

このDBのスキーマ管理はdashboard(Next.js)側のPrisma(dashboard/prisma/schema.prisma)に
一本化しており、ここではraw SQLで読み書きするのみでマイグレーションは行わない
(同一DBに対する二重のマイグレーション履歴を避けるため)。接続文字列は
dashboard側が使っているのと同じDATABASE_URL環境変数を共有する想定。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeoutを明示しないとハングしうる(2026-08-16、実地検証で確認)。
    #
    # options="-c timezone=UTC"(shirokuma-secレビューWARN対応、2026-08-16、
    # src/email_reminders/db.pyと同じ理由で一貫性のため追加): このモジュールが読み書きする
    # `sentAt`等はタイムゾーン情報を持たないPostgres `TIMESTAMP(3)`列で、UTC値として
    # 保存・比較される前提のコードが呼び出し元に多い。この前提をNeon側のセッション
    # TimeZone設定に暗黙に依存させず、接続確立時に明示的にUTCへ固定する。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


@dataclass(frozen=True)
class RepGmailConnection:
    rep_email: str
    refresh_token_enc: str
    last_synced_at: datetime | None
    # 2026-08-16、Gmail Push通知対応で追加。どちらも未設定(None)ならPush未登録/一度も
    # フル同期していない状態を表す(sync.sync_rep_incremental()参照)。
    history_id: str | None = None
    watch_expiration: datetime | None = None


def list_gmail_connections() -> list[RepGmailConnection]:
    """Gmail連携済みの営業担当を全件返す。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "repEmail", "refreshTokenEnc", "lastSyncedAt", "historyId", "watchExpiration" '
            'FROM "RepGmailConnection"'
        )
        rows = cur.fetchall()
    return [
        RepGmailConnection(
            rep_email=row["repEmail"],
            refresh_token_enc=row["refreshTokenEnc"],
            last_synced_at=row["lastSyncedAt"],
            history_id=row["historyId"],
            watch_expiration=row["watchExpiration"],
        )
        for row in rows
    ]


def find_connection_by_email(rep_email: str) -> RepGmailConnection | None:
    """Pub/Sub通知の`emailAddress`から該当担当のGmail連携を1件引く(2026-08-16、
    `gmail_push_webhook.py`向け)。見つからなければNoneを返す。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "repEmail", "refreshTokenEnc", "lastSyncedAt", "historyId", "watchExpiration" '
            'FROM "RepGmailConnection" WHERE "repEmail" = %s',
            (rep_email,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return RepGmailConnection(
        rep_email=row["repEmail"],
        refresh_token_enc=row["refreshTokenEnc"],
        last_synced_at=row["lastSyncedAt"],
        history_id=row["historyId"],
        watch_expiration=row["watchExpiration"],
    )


def update_last_synced_at(rep_email: str, when: datetime) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'UPDATE "RepGmailConnection" SET "lastSyncedAt" = %s WHERE "repEmail" = %s',
            (when, rep_email),
        )
        conn.commit()


def update_history_id(rep_email: str, history_id: str | None) -> None:
    """`sync.sync_rep_incremental()`が増分取得のたびに`historyId`だけを更新する(2026-08-16)。
    `watchExpiration`はwatch登録・延長時(`update_watch_state()`/`update_watch_expiration()`)
    のみ更新対象のため触れない。

    `history_id=None`は、`HistoryIdExpiredError`発生時に古い(もう使えない)値をクリアする
    ために使う(shirokuma-secレビューWARN対応: クリアしておかないと、次回の
    `watch_registration.register_or_renew_watch()`が「既にhistoryId設定済み」と誤認し、
    有効な値へ再ブートストラップされなくなってしまうため)。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'UPDATE "RepGmailConnection" SET "historyId" = %s WHERE "repEmail" = %s',
            (history_id, rep_email),
        )
        conn.commit()


def update_watch_expiration(rep_email: str, expiration: datetime) -> None:
    """`watch_registration.register_or_renew_watch()`の延長時(既に`historyId`が保存済みの
    場合)、`watchExpiration`のみを更新する(2026-08-16、shirokuma-secレビューWARN対応)。

    延長のたびに`historyId`も無条件で上書きすると、`sync_rep_incremental()`側の増分同期が
    何日も失敗し続けている間に日次のwatch延長が走った場合、未処理のバックログを飛び越えて
    `historyId`がリセットされてしまい、恒久的なメール見逃しにつながる。そのため延長では
    `historyId`には触れない(初回登録時のみ`update_watch_state()`で設定する)。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'UPDATE "RepGmailConnection" SET "watchExpiration" = %s WHERE "repEmail" = %s',
            (expiration, rep_email),
        )
        conn.commit()


def update_watch_state(rep_email: str, history_id: str, expiration: datetime) -> None:
    """`watch_registration.register_or_renew_watch()`の初回登録時(`historyId`未設定の場合)
    のみ呼ばれ、Push登録結果(`historyId`/`watchExpiration`)を保存する(2026-08-16)。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'UPDATE "RepGmailConnection" SET "historyId" = %s, "watchExpiration" = %s WHERE "repEmail" = %s',
            (history_id, expiration, rep_email),
        )
        conn.commit()


def email_log_exists(gmail_message_id: str) -> bool:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT 1 FROM "EmailLog" WHERE "gmailMessageId" = %s', (gmail_message_id,))
        return cur.fetchone() is not None


def insert_email_log(
    *,
    contact_page_id: str,
    contact_email: str,
    rep_email: str,
    gmail_message_id: str,
    direction: str,
    subject: str | None,
    snippet: str | None,
    sent_at: datetime,
) -> None:
    """`gmailMessageId`の一意制約により、既に記録済みのメールを渡すと例外になる
    (呼び出し元がemail_log_exists()で事前に重複排除する設計、meeting_syncの
    Googleカレンダーイベントid重複チェックと同じパターン)。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "EmailLog"
                (id, "contactPageId", "contactEmail", "repEmail", "gmailMessageId",
                 direction, subject, snippet, "sentAt", "createdAt")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                uuid.uuid4().hex,
                contact_page_id,
                contact_email,
                rep_email,
                gmail_message_id,
                direction,
                subject,
                snippet,
                sent_at,
            ),
        )
        conn.commit()
