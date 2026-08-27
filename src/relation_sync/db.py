"""ClientNameIndex/RelationReviewQueueテーブル(Neon Postgres)への直接アクセス(2026-08-25)。

`src/project_mirror/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側のPrisma
(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで読み書きするのみで
マイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を共有する。
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src import db_utils
from src.db_utils import db_truncated_utcnow

logger = logging.getLogger(__name__)

# 1回のINSERT文に含める行数の上限(src/project_mirror/db.pyと同じ目安)。
_UPSERT_BATCH_SIZE = 500

# `try_acquire_refresh_lock()`/`release_refresh_lock()`が使うPostgresアドバイザリロックの
# キー。project_mirror/db.pyの_REFRESH_LOCK_KEY(917_263_540)と衝突しない値を使う
# (値そのものに意味は無く、他用途と衝突しなければよい)。
_REFRESH_LOCK_KEY = 917_263_541


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeoutを明示しないとハングしうる(src/project_mirror/db.pyと同じ理由)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def _chunked(records: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [records[i : i + size] for i in range(0, len(records), size)]


def try_acquire_refresh_lock() -> psycopg.Connection[dict[str, Any]] | None:
    """`refresh_all_client_names()`の多重実行防止用に`pg_try_advisory_lock()`を試みる
    (project_mirror/db.pyのtry_acquire_refresh_lock()と同じ設計)。

    取得できた場合はロックを保持したままの`Connection`を返す(呼び出し元は処理完了後に必ず
    `release_refresh_lock()`でこの接続ごと解放すること)。取得できなかった場合(既に別
    プロセスが実行中)は`None`を返す。

    通常のCRUD用の`_connect()`とは異なり、`db_utils.connect_for_advisory_lock()`
    (`DATABASE_URL_UNPOOLED`優先)を使う。理由は`docs/relation_sync_activation_note.md`の
    「advisory lockは非pooled接続でのみ機能する」参照(2026-08-28)。

    `cur.execute()`が例外を投げた場合も接続をcloseしてから再送出する(呼び出し元はまだ
    `Connection`を受け取っていないため、ここでcloseしないと接続がリークする。
    `src/project_mirror/db.py`の`try_acquire_refresh_lock()`と同じ形の既存バグで、
    まとめて修正した。レビューWARN対応、2026-08-28)。
    """
    conn = db_utils.connect_for_advisory_lock(logger)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (_REFRESH_LOCK_KEY,))
            row = cur.fetchone()
    except Exception:
        conn.close()
        raise
    if not (row and row["locked"]):
        conn.close()
        return None
    return conn


def release_refresh_lock(conn: psycopg.Connection[dict[str, Any]]) -> None:
    """`try_acquire_refresh_lock()`で取得したロックを解放し、接続を閉じる。"""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_REFRESH_LOCK_KEY,))
    finally:
        conn.close()


def upsert_client_name(record: dict[str, Any]) -> None:
    """1件のClientNameIndexをUPSERTする(Notion Webhookからのリアルタイム更新用)。

    `record`は{"notion_page_id": str, "normalized_name": str, "raw_name": str}の形。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "ClientNameIndex" (id, "notionPageId", "normalizedName", "rawName", "syncedAt")
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT ("notionPageId") DO UPDATE SET
                "normalizedName" = EXCLUDED."normalizedName",
                "rawName" = EXCLUDED."rawName",
                "syncedAt" = now()
            """,
            (
                uuid.uuid4().hex,
                record["notion_page_id"],
                record["normalized_name"],
                record["raw_name"],
            ),
        )
        conn.commit()


def upsert_client_names_and_sweep(records: list[dict[str, Any]]) -> int:
    """取引先マスターDB全件をインデックスへ反映する(バックフィル・夜間reconciliation共通)。

    `src/project_mirror/db.py`の`upsert_projects_and_sweep()`と同じmark-and-sweep方式
    (1トランザクション内で全件UPSERT後、今回のバッチで触れられなかった行をDELETE)。
    戻り値は実際に削除された行数。

    `records`が空の場合、呼び出し元の取得結果が空リストである可能性が高く、そのまま
    sweepするとインデックス全件を削除してしまう事故になりうるため、何もせずwarningログのみ
    出して早期リターンする(project_mirror/db.pyと同じ安全側の判断。戻り値は0)。

    基準時刻には`db_truncated_utcnow()`(`src/db_utils.py`)を使う。理由は
    `project_mirror/db.py`の`upsert_projects_and_sweep()`のdocstring参照
    (`syncedAt`が`TIMESTAMP(3)`のため、素のマイクロ秒精度の値だと保存時のPostgresの
    丸め(四捨五入で繰り上がる場合がある)により挿入直後の行が誤ってDELETEされる事故が
    本番で発生した、2026-08-25)。
    """
    if not records:
        logger.warning(
            "upsert_client_names_and_sweep: records is empty; スキップします"
            "(意図しない全件削除を避けるため、空リストではインデックスを更新しません)"
        )
        return 0

    synced_at = db_truncated_utcnow()
    with _connect() as conn:
        with conn.cursor() as cur:
            for batch in _chunked(records, _UPSERT_BATCH_SIZE):
                _upsert_batch(cur, batch, synced_at=synced_at)
            cur.execute(
                'DELETE FROM "ClientNameIndex" WHERE "syncedAt" < %s',
                (synced_at,),
            )
            deleted_count = cur.rowcount
        conn.commit()
    return deleted_count


def _upsert_batch(
    cur: psycopg.Cursor[dict[str, Any]], batch: list[dict[str, Any]], *, synced_at: datetime
) -> None:
    values_sql = ", ".join(["(%s, %s, %s, %s, %s)"] * len(batch))
    params: list[Any] = []
    for record in batch:
        params.extend(
            [
                uuid.uuid4().hex,
                record["notion_page_id"],
                record["normalized_name"],
                record["raw_name"],
                synced_at,
            ]
        )
    cur.execute(
        f"""
        INSERT INTO "ClientNameIndex" (id, "notionPageId", "normalizedName", "rawName", "syncedAt")
        VALUES {values_sql}
        ON CONFLICT ("notionPageId") DO UPDATE SET
            "normalizedName" = EXCLUDED."normalizedName",
            "rawName" = EXCLUDED."rawName",
            "syncedAt" = EXCLUDED."syncedAt"
        """,
        params,
    )


def find_by_normalized_name(normalized_name: str) -> list[dict[str, Any]]:
    """正規化済み取引先名の完全一致でClientNameIndexを検索する。

    同じ正規化キーを持つ取引先が複数存在しうる(表記ゆれではなく実在する別会社が同名寄せ
    されるケース)ため、`list[dict]`を返す(0件・1件・複数件のいずれもありうる。曖昧な
    解決を自動確定しないかどうかの判断は呼び出し元`src/relation_sync/resolve.py`の責務)。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "notionPageId", "rawName" FROM "ClientNameIndex" WHERE "normalizedName" = %s',
            (normalized_name,),
        )
        rows = cur.fetchall()
    return [
        {"notion_page_id": row["notionPageId"], "raw_name": row["rawName"]} for row in rows
    ]


def get_client_name_count() -> int:
    """`ClientNameIndex`の現在の行数。`refresh_all_client_names()`が新規取得件数と比較し、
    異常な急減(部分取得によるsweep事故)を検知するために使う(project_mirror/sync.pyの
    get_project_count()と同じ用途)。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT count(*) AS n FROM "ClientNameIndex"')
        return cur.fetchone()["n"]
