"""シートの行を作れなかったレコードを積んでおき、後から作り直すためのキュー（2026-09-07）。

■ 名前について（先に断っておく）

**いわゆる transactional outbox パターンではない。** 主DBの書き込みと同一トランザクションで
イベントを書いて配送を保証する仕組みではなく、**外部API（Sheets）への書き込みが失敗した
あとに積む、再試行キュー（デッドレターキュー）**。積むこと自体が失敗しうる
（下の「積むのは最善努力」）。配送保証があると思って読まないこと。

■ 何を解いているのか

同期エンジンは「シートの行を作れなかった」ときもWebhookに **2xx** を返す。
Notionページと`IdMapping`は既にできているので、500を返してリトライさせると
**重複ページを作りかねない経路**（`_try_create_new_record()`）を叩き直すことになるためで、
これ自体は正しい判断だった。ただしその代償として、拾う手段が人任せだった。

```
   これまで                                  ここで足すもの
   ─────────────────────────────────         ─────────────────────────────
   Slack通知                     人が読む     ★このキューに積む
   verify_spreadsheet_backfill   人が流す     ★日次のcronが作り直す
   そのレコードの次の更新         いつ来るか不定
   ＝ 見落とし＋更新が来なければ永久に欠ける   ＝ 放っておいても埋まる
```

■ **値は持たない。持つのは「どのレコードか」だけ**

作成時点のスナップショットを貯めて後から流すと、**その間に入った新しい値を
古い値で巻き戻す**（`Dispatcher._append_spreadsheet_row_for_created_record()`の
`append_only`と同じ事故）。行を作るときは必ずNotion（マスター）を読み直す
（`src/sync_engine/spreadsheet_outbox_drain.py`）。

そのため、このキューが直せるのは **「行がまるごと無い」** 状態だけ。
「行はあるが1列だけ書けなかった」は対象外で、そちらは従来どおり
イベントの再送（500）と次の更新イベントで直る。

■ 積むのは最善努力。**積めたかどうかを呼び出し元に返す**

ここでの失敗（DBが落ちている等）でWebhook処理を止めない
（`webhook_receipts.py`と同じ）。ただし黙って捨てると元の穴に戻るので、
`enqueue_row_creation()`は**積めたかを返し**、呼び出し元はSlackの文面を
「自動で作り直します」と「手動のバックフィルが要ります」に書き分ける。

■ 溜め続けない

解決した行は`purge_resolved()`で消す（ポーリング型の取り込みで
「stateを無期限に蓄積しない」としたのと同じ理由）。
`failed`（諦めた行）は**消さない**。人が対処するために残す。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

#: 未処理。取り出しの対象。
STATUS_PENDING = "pending"
#: 解決済み（行ができた／もう要らないと分かった）。掃除の対象。
STATUS_DONE = "done"
#: 諦めた。**人の対処が要る。** 掃除しない。
STATUS_FAILED = "failed"

#: どう決着したか（`status`とは別に持つ）。`done`だけだと「行を作れた」と
#: 「そもそも作る必要が無かった」が混ざり、**outboxが実際に効いているかを数えられない**
#: （2026-09-07、おばさん指摘）。
RESOLUTION_CREATED = "created"
RESOLUTION_ALREADY_PRESENT = "already_present"
RESOLUTION_NOT_APPLICABLE = "not_applicable"
RESOLUTION_GAVE_UP = "gave_up"
#: 人が`backfill_spreadsheet_rows.py`で作ったのを、後から`sweep_failed()`が見つけた。
RESOLUTION_RECOVERED_MANUALLY = "recovered_manually"

#: 積む理由。**リテラルを散らさず、ここを唯一の一覧にする**（2026-09-07、クマ指摘）。
#: `docs/spreadsheet_outbox_note.md`の表と1対1で対応し、テストが機械的に突き合わせる。
#: 数えるためのラベルなので、増やすときは必ずドキュメントの表も足すこと。
REASON_NEW_RECORD_ROW_WRITE_FAILED = "new_record_row_write_failed"
REASON_NEW_RECORD_ROW_WRITE_SKIPPED = "new_record_row_write_skipped"
REASON_NEW_RECORD_ROW_PROPERTIES_UNAVAILABLE = "new_record_row_properties_unavailable"
REASON_UPDATE_NOTION_FETCH_FAILED = "update_notion_fetch_failed"
REASON_UPDATE_NOTION_PAGE_MISSING = "update_notion_page_missing"
REASON_UPDATE_SCHEMA_UNAVAILABLE = "update_schema_unavailable"
REASON_UPDATE_ROW_WRITE_SKIPPED = "update_row_write_skipped"
REASON_UPDATE_ROW_WRITE_ERROR = "update_row_write_error"

#: 上の全部。ドキュメントとの突き合わせに使う。
REASONS: frozenset[str] = frozenset(
    {
        REASON_NEW_RECORD_ROW_WRITE_FAILED,
        REASON_NEW_RECORD_ROW_WRITE_SKIPPED,
        REASON_NEW_RECORD_ROW_PROPERTIES_UNAVAILABLE,
        REASON_UPDATE_NOTION_FETCH_FAILED,
        REASON_UPDATE_NOTION_PAGE_MISSING,
        REASON_UPDATE_SCHEMA_UNAVAILABLE,
        REASON_UPDATE_ROW_WRITE_SKIPPED,
        REASON_UPDATE_ROW_WRITE_ERROR,
    }
)

#: この回数だけ試して駄目なら`failed`にする。1日1回のcronなので、およそ1週間ぶん。
MAX_ATTEMPTS = 8

#: `done`を残しておく日数。行ができたことの記録が要るのは、直後の調査のときだけ。
RESOLVED_RETENTION_DAYS = 30

#: 取り出しのたびに次回を後ろへ下げる（5分 × 3^n、5回目以降は頭打ちでおよそ20時間）。
#: cronが日次のうちは効きどころが少ないが、手で叩いたときと将来cronを増やしたときに効く。
_BACKOFF_BASE_MINUTES = 5
_BACKOFF_MAX_EXPONENT = 5

_missing_database_url_warned = False


@dataclass(frozen=True)
class OutboxEntry:
    """取り出した1件。**値は持たない**（モジュールdocstring参照）。"""

    db_key: str
    notion_key: str
    reason: str
    attempts: int
    created_at: datetime


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeout/options: 他の`db.py`群と同じ理由（ハング防止・UTC固定）。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def _database_configured() -> bool:
    """DBが無い環境（ローカル・テスト）では何もしない。警告は1プロセスに1回だけ。"""
    global _missing_database_url_warned

    if os.environ.get("DATABASE_URL"):
        return True
    if not _missing_database_url_warned:
        logger.warning(
            "spreadsheet outbox: DATABASE_URLが未設定のため、行を作れなかったレコードを"
            "積めません。**本番でこの警告が出ている場合、取りこぼしは自動では復旧しません**"
        )
        _missing_database_url_warned = True
    return False


def reset_missing_database_url_warning() -> None:
    """テスト用。警告は1プロセスに1回だけ出す作りのため、テスト間でリセットする。"""
    global _missing_database_url_warned
    _missing_database_url_warned = False


def _is_broken_sql(exc: BaseException) -> bool:
    """コードのバグ（SQLの構文・型・列名の間違い）か。**時間が経っても直らない類。**

    2026-09-07、Geminiの指摘。ここでの`except`は本来「DBが一時的に落ちている」を
    握るためのもので、**構文エラーまで握ると、デプロイ直後から1件も積まれていないのに
    誰も気づけない**。種類を分けて、バグの側は`error`で出す（`warning`はログに埋もれる）。
    """
    return isinstance(exc, psycopg.ProgrammingError)


def _log_failure(message: str, exc: BaseException, **context: Any) -> None:
    """失敗を記録する。**バグと一時障害でログの重さを変える。**"""
    if _is_broken_sql(exc):
        logger.error(
            "spreadsheet outbox: %s **SQLが壊れています（コードの修正が必要。"
            "この状態では1件も処理されません）** %r",
            message,
            context,
            exc_info=True,
        )
        return
    logger.warning("spreadsheet outbox: %s %r", message, context, exc_info=True)


def enqueue_row_creation(*, db_key: str, notion_key: str, reason: str) -> bool:
    """このレコードの行を作り直す必要がある、と積む。**積めたらTrue。**

    同じレコードを何度積んでも行は増えない（主キーが`(dbKey, notionKey)`）。

    既に`pending`の行があるときは**回数と次回時刻を据え置く**。据え置かないと、
    失敗が続くレコードほど頻繁にイベントが来て、後退させたはずのバックオフが
    毎回リセットされる。逆に`done`/`failed`だった行は、新しい取りこぼしなので
    最初から数え直す。
    """
    if not _database_configured():
        return False
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO "SpreadsheetOutbox"
                    ("dbKey", "notionKey", status, reason, attempts,
                     "nextAttemptAt", "createdAt", "updatedAt")
                VALUES (%s, %s, %s, %s, 0, now(), now(), now())
                -- **同じ値を何度も渡さず`EXCLUDED`で引く**（2026-09-07、Geminiの指摘）。
                -- `EXCLUDED.status`は必ず`pending`（上のVALUES）なので、
                -- 「今すでにpendingか」の判定にもそのまま使える。
                ON CONFLICT ("dbKey", "notionKey") DO UPDATE
                SET status = EXCLUDED.status,
                    reason = EXCLUDED.reason,
                    "lastError" = NULL,
                    resolution = NULL,
                    "resolvedAt" = NULL,
                    "updatedAt" = now(),
                    attempts = CASE
                        WHEN "SpreadsheetOutbox".status = EXCLUDED.status
                        THEN "SpreadsheetOutbox".attempts
                        ELSE 0
                    END,
                    "nextAttemptAt" = CASE
                        WHEN "SpreadsheetOutbox".status = EXCLUDED.status
                        THEN "SpreadsheetOutbox"."nextAttemptAt"
                        ELSE now()
                    END
                """,
                (db_key, notion_key, STATUS_PENDING, reason),
            )
            conn.commit()
        logger.info(
            "spreadsheet outbox: 行の作り直しを積みました (db_key=%r, notion_key=%r, reason=%r)",
            db_key,
            notion_key,
            reason,
        )
        return True
    except Exception as exc:  # noqa: BLE001 (積めなくてもWebhook処理は止めない)
        # **ここでは投げ直さない。** 呼び出し元はWebhookの処理中で、例外を通すと
        # 「Notionページは作ったのに500」になり、リトライで重複ページを作る経路を叩く。
        # 代わりに`False`を返し、Slackの文面が「積めませんでした＝自動では復旧しません」に
        # 変わることで人に伝わる（`_handle_new_record_row_not_created()`）。
        _log_failure(
            "積めませんでした。**このレコードは自動では復旧しません**",
            exc,
            db_key=db_key,
            notion_key=notion_key,
            reason=reason,
        )
        return False


def claim_due(*, db_keys: Sequence[str], limit: int) -> list[OutboxEntry]:
    """再試行時刻が来た`pending`を取り出す。**取り出した時点で回数を増やす。**

    増やしてから処理するのは、処理中に落ちた回を数え漏らさないため
    （数え漏らすと、必ず落ちるレコードが永久に再試行され続ける）。
    同時に`nextAttemptAt`も先へ進めるので、`record_failure()`を呼べずに
    落ちた場合でも、次のcronがすぐ同じ行を掴み直すことはない。

    `db_keys`は**いま行の新規作成が許可されているDB**だけを渡す。許可されていないDBの
    行は`pending`のまま置いておく（作りに行っても`append_with_sync_key()`が弾くので、
    試行回数だけ空に減って`failed`になってしまう）。
    """
    if not db_keys or limit <= 0 or not _database_configured():
        return []
    try:
        with _connect() as conn, conn.cursor() as cur:
            # **ロックを取る側をCTEで先に確定させる**（2026-09-07、Geminiの指摘）。
            # `UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED)` はプランナーの
            # 解釈次第でサブクエリ側と外側のUPDATEが二重にロックを取りに行きうる。
            # CTEなら「誰を取るか」が先に閉じるので、この曖昧さが無い。
            #
            # **プレースホルダには明示的にキャストを添える**（同じくGeminiの指摘）。
            # `make_interval(mins => ...)`や`power()`の引数のように、周りから型を
            # 決めきれない位置に裸の`%s`を置くと、Postgresが型を決められず
            # `could not determine data type of parameter`で落ちる。
            # **落ちても下のexceptが握って「0件」に見えるだけ**なので、静かに壊れる。
            cur.execute(
                """
                WITH due AS (
                    SELECT "dbKey", "notionKey"
                    FROM "SpreadsheetOutbox"
                    WHERE status = %s
                      AND "nextAttemptAt" <= now()
                      AND "dbKey" = ANY(%s::text[])
                    ORDER BY "nextAttemptAt"
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE "SpreadsheetOutbox" AS o
                SET attempts = o.attempts + 1,
                    -- 失敗するほど次回を後ろへ（5分 × 3^試行回数、頭打ちあり）。
                    "nextAttemptAt" = now() + make_interval(
                        mins => (%s::int * power(3, least(o.attempts, %s::int)))::int
                    ),
                    "updatedAt" = now()
                FROM due
                WHERE o."dbKey" = due."dbKey" AND o."notionKey" = due."notionKey"
                RETURNING o."dbKey", o."notionKey", o.reason, o.attempts, o."createdAt"
                """,
                (
                    STATUS_PENDING,
                    list(db_keys),
                    limit,
                    _BACKOFF_BASE_MINUTES,
                    _BACKOFF_MAX_EXPONENT,
                ),
            )
            rows = cur.fetchall()
            conn.commit()
    except psycopg.ProgrammingError:
        # **ここは投げ直す**（2026-09-07、Geminiの指摘）。呼び出し元はcronなので、
        # 落とせばレスポンスが500になって気づける。握って`[]`を返すと
        # 「今日は積まれた分が無かった」と区別がつかず、**壊れたまま毎日静かに空回りする**。
        logger.error(
            "spreadsheet outbox: 取り出しのSQLが壊れています（コードの修正が必要）",
            exc_info=True,
        )
        raise
    except Exception as exc:  # noqa: BLE001 (取り出せない回はスキップして次のcronに任せる)
        _log_failure("取り出しに失敗しました", exc)
        return []
    return [
        OutboxEntry(
            db_key=row["dbKey"],
            notion_key=row["notionKey"],
            reason=row["reason"],
            attempts=int(row["attempts"]),
            created_at=row["createdAt"],
        )
        for row in rows
    ]


def list_failed(*, db_keys: Sequence[str], limit: int) -> list[OutboxEntry]:
    """諦めた（`failed`）行を読み出す。**取り出しではないので回数は増やさない。**

    使うのは`sweep_failed()`（`spreadsheet_outbox_drain.py`）だけ。

    ■ なぜこれが要るのか（2026-09-07、おばさん指摘）

    `failed`になった行の案内は「`backfill_spreadsheet_rows.py`で作り直してください」だが、
    **そのスクリプトはこのテーブルを一切触らない**。案内どおりに直しても`failed`は残り、
    診断（`probe_spreadsheet_outbox`）は永久に赤いままになる。

    **消えない赤信号は、誤報と同じだけ有害。** 「誤報を鳴らし続けると、本物の通知も
    無視されるようになる」（CLAUDE.md）のを、この機能自身が起こしてしまう。
    そこで日次のcronが`failed`を見に行き、**行がもうできていれば静かに閉じる。**
    """
    if not db_keys or limit <= 0 or not _database_configured():
        return []
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT "dbKey", "notionKey", reason, attempts, "createdAt"
                FROM "SpreadsheetOutbox"
                WHERE status = %s AND "dbKey" = ANY(%s)
                ORDER BY "resolvedAt"
                LIMIT %s
                """,
                (STATUS_FAILED, list(db_keys), limit),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 (棚卸しの失敗で本体を止めない)
        _log_failure("諦めた行の読み出しに失敗しました", exc)
        return []
    return [
        OutboxEntry(
            db_key=row["dbKey"],
            notion_key=row["notionKey"],
            reason=row["reason"],
            attempts=int(row["attempts"]),
            created_at=row["createdAt"],
        )
        for row in rows
    ]


def mark_done(*, db_key: str, notion_key: str, resolution: str, note: str) -> None:
    """解決した。`resolution`で**どう決着したか**を分けて残す。

    `status=done`だけだと「行を作れた」と「そもそも作る必要が無かった」が混ざり、
    後から「outboxは実際に効いているのか」を数えられない（2026-09-07、おばさん指摘）。
    """
    _resolve(
        db_key=db_key,
        notion_key=notion_key,
        status=STATUS_DONE,
        resolution=resolution,
        last_error=note,
    )


def record_failure(*, db_key: str, notion_key: str, error: str) -> str:
    """今回の試行が失敗した。**上限に達していたら`failed`にして返す。**

    戻り値は結果の状態（`pending` か `failed`）。呼び出し元はこれを見て
    「まだ自動で再試行する」「人の対処が要る」を区別する。

    `error`には**例外の全文を渡さない**（接続先やユーザー名が載る）。
    種類だけを渡すこと。全文はサーバー側のログにある。
    """
    if not _database_configured():
        return STATUS_PENDING
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE "SpreadsheetOutbox"
                SET "lastError" = %s,
                    "updatedAt" = now(),
                    status = CASE WHEN attempts >= %s THEN %s ELSE status END,
                    resolution = CASE WHEN attempts >= %s THEN %s ELSE resolution END,
                    "resolvedAt" = CASE WHEN attempts >= %s THEN now() ELSE "resolvedAt" END
                WHERE "dbKey" = %s AND "notionKey" = %s
                RETURNING status
                """,
                (
                    error,
                    MAX_ATTEMPTS,
                    STATUS_FAILED,
                    MAX_ATTEMPTS,
                    RESOLUTION_GAVE_UP,
                    MAX_ATTEMPTS,
                    db_key,
                    notion_key,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return str(row["status"]) if row else STATUS_PENDING
    except Exception as exc:  # noqa: BLE001 (記録の失敗で全体を止めない)
        _log_failure("失敗の記録に失敗しました", exc, db_key=db_key, notion_key=notion_key)
        return STATUS_PENDING


def release(*, db_key: str, notion_key: str, retry_after_minutes: int) -> None:
    """今回は「試したうちに入らない」ので、回数を戻して早めに取り直す。

    別のワーカーが同じレコードの行を作成中でロックを取れなかった場合など、
    **こちらの都合ではない見送り**に使う。これを数に入れると、混み合っただけで
    `failed`（人の対処が要る）へ落ちてしまう。
    """
    if not _database_configured():
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE "SpreadsheetOutbox"
                SET attempts = GREATEST(attempts - 1, 0),
                    "nextAttemptAt" = now() + make_interval(mins => %s),
                    "updatedAt" = now()
                WHERE "dbKey" = %s AND "notionKey" = %s AND status = %s
                """,
                (retry_after_minutes, db_key, notion_key, STATUS_PENDING),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 (差し戻せなくても次のcronが拾う)
        _log_failure("差し戻しに失敗しました", exc, db_key=db_key, notion_key=notion_key)


def purge_resolved(days: int = RESOLVED_RETENTION_DAYS) -> int:
    """古い`done`を消す。**`failed`は消さない**（人の対処が要るものを消してしまわない）。"""
    if not _database_configured():
        return 0
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM "SpreadsheetOutbox"
                WHERE status = %s
                  AND "resolvedAt" IS NOT NULL
                  AND "resolvedAt" < now() - make_interval(days => %s)
                """,
                (STATUS_DONE, days),
            )
            deleted = cur.rowcount
            conn.commit()
        return int(deleted)
    except Exception as exc:  # noqa: BLE001 (掃除の失敗で全体を止めない)
        _log_failure("古い記録の掃除に失敗しました", exc)
        return 0


def outbox_stats() -> dict[str, Any]:
    """滞留の状況（診断用）。**例外は握らない**（診断側が`failed`として表示する）。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT status, count(*) AS count, min("createdAt") AS oldest
            FROM "SpreadsheetOutbox"
            GROUP BY status
            """
        )
        by_status = {
            str(row["status"]): {
                "count": int(row["count"]),
                "oldest_created_at": row["oldest"].isoformat() if row["oldest"] else None,
            }
            for row in cur.fetchall()
        }
        cur.execute(
            """
            SELECT "dbKey", count(*) AS count
            FROM "SpreadsheetOutbox"
            WHERE status = %s
            GROUP BY "dbKey"
            ORDER BY count DESC
            """,
            (STATUS_PENDING,),
        )
        pending_by_db = {str(row["dbKey"]): int(row["count"]) for row in cur.fetchall()}
        # **「効いているのか」を数えられるようにする。** `done`の件数だけでは
        # 「行を作れた」と「そもそも要らなかった」が混ざる（2026-09-07、おばさん指摘）。
        cur.execute(
            """
            SELECT resolution, count(*) AS count
            FROM "SpreadsheetOutbox"
            WHERE resolution IS NOT NULL
            GROUP BY resolution
            ORDER BY count DESC
            """
        )
        by_resolution = {str(row["resolution"]): int(row["count"]) for row in cur.fetchall()}
    return {
        "by_status": by_status,
        "by_resolution": by_resolution,
        "pending_by_db_key": pending_by_db,
    }


def _resolve(
    *, db_key: str, notion_key: str, status: str, resolution: str, last_error: str
) -> None:
    if not _database_configured():
        return
    try:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE "SpreadsheetOutbox"
                SET status = %s,
                    resolution = %s,
                    "lastError" = %s,
                    "resolvedAt" = now(),
                    "updatedAt" = now()
                WHERE "dbKey" = %s AND "notionKey" = %s
                """,
                (status, resolution, last_error, db_key, notion_key),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 (記録の失敗で全体を止めない)
        _log_failure(
            "状態の更新に失敗しました", exc, db_key=db_key, notion_key=notion_key, status=status
        )
