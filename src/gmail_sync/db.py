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
    incident_score: int | None = None,
    incident_priority: str | None = None,
    gmail_thread_id: str | None = None,
) -> None:
    """`gmailMessageId`の一意制約により、既に記録済みのメールを渡すと例外になる
    (呼び出し元がemail_log_exists()で事前に重複排除する設計、meeting_syncの
    Googleカレンダーイベントid重複チェックと同じパターン)。

    `incident_score`/`incident_priority`(2026-08-16、src/incident_detection/)は
    `direction == "inbound"`の場合のみ呼び出し元(sync.py)がスコアリング結果を渡す。
    それ以外(outbound、またはスコアリング対象外)はどちらもNoneのまま。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "EmailLog"
                (id, "contactPageId", "contactEmail", "repEmail", "gmailMessageId",
                 "gmailThreadId", direction, subject, snippet, "sentAt", "createdAt",
                 "incidentScore", "incidentPriority")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
            """,
            (
                uuid.uuid4().hex,
                contact_page_id,
                contact_email,
                rep_email,
                gmail_message_id,
                gmail_thread_id,
                direction,
                subject,
                snippet,
                sent_at,
                incident_score,
                incident_priority,
            ),
        )
        conn.commit()


def fetch_existing_message_ids() -> set[str]:
    """記録済みの`gmailMessageId`を全件返す(2026-09-03、過去分の一括取り込み用)。

    `email_log_exists()`はメール1通ごとに接続を張るため、数千通を辿る取り込みでは
    接続コストだけで現実的な時間に収まらない。取り込み開始時に1回だけ全件読み、
    以降はメモリ上の集合で重複排除する。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT "gmailMessageId" FROM "EmailLog"')
        return {row["gmailMessageId"] for row in cur.fetchall()}


@dataclass(frozen=True)
class EmailLogRow:
    """`insert_email_logs()`へ渡す1行分。"""

    contact_page_id: str
    contact_email: str
    rep_email: str
    gmail_message_id: str
    direction: str
    subject: str | None
    snippet: str | None
    sent_at: datetime
    gmail_thread_id: str | None = None


def insert_email_logs(rows: list[EmailLogRow]) -> int:
    """`EmailLog`へまとめて追記する(2026-09-03、過去分の一括取り込み用)。実際に
    挿入できた件数を返す。

    `insert_email_log()`との違いは3点。

    ```
       接続        1行1接続            →  まとめて1接続1トランザクション
       重複        例外になる           →  ON CONFLICT DO NOTHINGで黙って飛ばす
       インシデント 呼び出し元が渡す      →  常にNULL（過去分にスコアを付けない）
    ```

    **インシデントスコアを常にNULLにするのは意図的。** `incidentPriority='medium'`かつ
    `digestedAt IS NULL`の行は日次ダイジェストに載るが、この抽出には日付の絞り込みが
    無い(`src/incident_detection/db.py`)。過去数か月分にスコアを付けると、取り込んだ
    直後のダイジェストに過去のインシデントが一斉に載る。過去分に対して「今起きたこと」
    として反応させないための封じ込め。
    """
    if not rows:
        return 0
    with _connect() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO "EmailLog"
                (id, "contactPageId", "contactEmail", "repEmail", "gmailMessageId",
                 "gmailThreadId", direction, subject, snippet, "sentAt", "createdAt")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT ("gmailMessageId") DO NOTHING
            """,
            [
                (
                    uuid.uuid4().hex,
                    r.contact_page_id,
                    r.contact_email,
                    r.rep_email,
                    r.gmail_message_id,
                    r.gmail_thread_id,
                    r.direction,
                    r.subject,
                    r.snippet,
                    r.sent_at,
                )
                for r in rows
            ],
        )
        inserted = cur.rowcount
        conn.commit()
    return inserted


def fetch_email_events_by_contact_page_ids(page_ids: list[str]) -> list[dict[str, Any]]:
    """指定した連絡先の送受信ログを、返信傾向の分析(`src/analytics/reply_timing.py`)向けに
    必要最小限の列だけ読む(2026-09-03)。

    件名・スニペット(メール内容そのもの)は読まない。時間帯とラグを数えるのに要らず、
    ログ・キャッシュへ漏れる面を狭くしておくため。
    """
    if not page_ids:
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "contactPageId", "contactEmail", "gmailThreadId", direction, "sentAt" '
            'FROM "EmailLog" WHERE "contactPageId" = ANY(%s) ORDER BY "sentAt"',
            (page_ids,),
        )
        return cur.fetchall()


def fetch_oldest_email_sent_at() -> datetime | None:
    """`EmailLog`の最も古い`sentAt`を返す（1行も無ければNone、2026-09-03）。

    過去分の取り込み（`scripts/backfill_gmail_history.py`）が「通常同期が面倒を見ている
    期間」の始まりを知るために使う。ここより前だけを取り込めば、同じメールを取り合って
    通常同期の副作用（インシデント判定・Notion更新・外部通知）を打ち消すことがない。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT min("sentAt") AS oldest FROM "EmailLog"')
        row = cur.fetchone()
        return row["oldest"] if row else None
