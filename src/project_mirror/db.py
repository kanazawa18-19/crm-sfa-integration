"""ProjectMirrorテーブル(Neon Postgres)への直接アクセス(2026-08-17)。

`src/audit_log/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで読み書きするのみで
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
from psycopg.types.json import Json

from src import db_utils
from src.db_utils import db_truncated_utcnow

logger = logging.getLogger(__name__)

# 1回のINSERT文に含める行数の上限。Postgresのパラメータ数上限(65535)には遠く及ばないが、
# 1クエリが大きくなりすぎない程度の目安としてこの値を使う(src/migration/のバルク処理と
# 同程度の粒度)。
_UPSERT_BATCH_SIZE = 500

# `try_acquire_refresh_lock()`/`release_refresh_lock()`が使うPostgresアドバイザリロックの
# キー(任意のbigint。他用途と衝突しなければ値そのものに意味は無い)。
_REFRESH_LOCK_KEY = 917_263_540


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeoutを明示しないとハングしうる(src/gmail_sync/db.pyと同じ理由)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def _chunked(records: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [records[i : i + size] for i in range(0, len(records), size)]


def try_acquire_refresh_lock() -> psycopg.Connection[dict[str, Any]] | None:
    """`refresh_all_projects()`の多重実行防止用に`pg_try_advisory_lock()`を試みる
    (shirokuma-secレビューWARN対応、2026-08-17)。

    夜間reconciliation cronと手動バックフィルスクリプト(`scripts/backfill_project_mirror.py`)
    が偶発的に重なると、後から完了した方の`upsert_projects_and_sweep()`が古い実行の
    `syncedAt`で新しいデータを上書き・sweepしてしまう恐れがある。取得できた場合は
    ロックを保持したままの`Connection`を返す(セッション単位のロックのため、呼び出し元は
    処理完了後に必ず`release_refresh_lock()`でこの接続ごと解放すること)。取得できなかった
    場合(既に別プロセスが実行中)は`None`を返す。

    `cur.execute()`が例外を投げた場合も接続をcloseしてから再送出する(呼び出し元はまだ
    `Connection`を受け取っていないため、ここでcloseしないと接続がリークする。
    `src/document_generation/approval_db.py`の`try_acquire_approval_lock()`と同じ形の
    既存バグで、両方まとめて修正した。QAレビューWARN対応、2026-08-28)。

    通常のCRUD用の`_connect()`とは異なり、`db_utils.connect_for_advisory_lock()`
    (`DATABASE_URL_UNPOOLED`優先)を使う。理由は`docs/project_mirror_activation_note.md`の
    「advisory lockは非pooled接続でのみ機能する」参照(2026-08-28)。
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


def upsert_project(record: dict[str, Any]) -> None:
    """1件のProjectMirrorをUPSERTする(Notion Webhookからのリアルタイム更新用)。

    `record`は{"notion_page_id": str, "data": dict, "last_edited_at": datetime | None}の形。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "ProjectMirror" (id, "notionPageId", data, "lastEditedAt", "syncedAt")
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT ("notionPageId") DO UPDATE SET
                data = EXCLUDED.data,
                "lastEditedAt" = EXCLUDED."lastEditedAt",
                "syncedAt" = now()
            """,
            (
                uuid.uuid4().hex,
                record["notion_page_id"],
                Json(record["data"]),
                record.get("last_edited_at"),
            ),
        )
        conn.commit()


def upsert_projects(records: list[dict[str, Any]], *, synced_at: datetime) -> None:
    """掃除せずにUPSERTだけ行う（分割実行の途中で呼ぶ、2026-09-01）。

    案件管理DBは26,017件あり、1回の実行（Vercelの上限300秒）では取り切れないため、
    何回かに分けて取り込む。**途中で掃除してはいけない**（まだ見ていないだけの行を
    消してしまう。ProjectMirrorを全消失させた事故と同じ形）。掃除は一巡し終えたときに
    `sweep_projects()`で行う。`src/relation_sync/db.py`の`upsert_client_names()`と同じ形。

    `synced_at`には**一巡を始めた時刻**（`SyncCursor.pass_started_at`）を渡すこと。
    末尾の`sweep_projects(before=...)`が同じ時刻を基準に「今回の一巡で触れなかった行」を
    消すため、両者がズレると取り込み済みの行まで消える。
    """
    if not records:
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            for batch in _chunked(records, _UPSERT_BATCH_SIZE):
                _upsert_batch(cur, batch, synced_at=synced_at)
        conn.commit()


def sweep_projects(*, before: datetime) -> int:
    """一巡で触れられなかった行を消す。削除件数を返す（2026-09-01）。

    **一巡し終えたときにだけ呼ぶこと。** 途中で呼ぶと、まだ取り込んでいないだけの行を
    消してしまう（2026-08-25にProjectMirrorを全消失させた事故と同じ形）。
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute('DELETE FROM "ProjectMirror" WHERE "syncedAt" < %s', (before,))
            deleted = cur.rowcount
        conn.commit()
    return deleted


def upsert_projects_and_sweep(records: list[dict[str, Any]]) -> int:
    """案件管理DB全件をミラーへ反映する(バックフィル・夜間reconciliation共通)。

    1トランザクション内で全件UPSERT後、今回のバッチで触れられなかった(＝Notion側で削除
    された)行を`syncedAt`が今回のバッチ開始時刻より古いものとしてDELETEするmark-and-sweep
    方式。500件程度のバッチでVALUES一括INSERTする。戻り値は実際に削除された行数
    (`scripts/backfill_project_mirror.py`が出力に含める、obasan-qualityレビューWARN対応、
    2026-08-17)。

    `records`が空の場合、呼び出し元(`refresh_all_projects`)の取得結果が空リストである
    可能性が高く、そのままsweepするとミラー全件を削除してしまう事故になりうるため、
    何もせずwarningログのみ出して早期リターンする(安全側に倒す。戻り値は0)。

    基準時刻には`datetime.now(timezone.utc)`をそのまま使わず`db_truncated_utcnow()`
    (`src/db_utils.py`)を使う。`syncedAt`カラムは`TIMESTAMP(3)`(ミリ秒精度)のため、
    素のマイクロ秒精度の値をUPSERTすると保存時にPostgresが四捨五入(round-half-up、
    境界によっては繰り上がる)でミリ秒精度に丸める一方、末尾のDELETEの比較には丸められて
    いない元の値が使われ、丸め方向次第で`保存値 < 比較用の元の値`が真になって挿入直後の
    行まで誤削除される事故が本番で発生した(2026-08-25、実データで再現確認済み)。
    `db_truncated_utcnow()`が値を事前にミリ秒境界(1000の倍数)へ切り捨てておくことで、
    Postgres側の丸めが切り捨てでも繰り上げでも結果が変わらない不動点になり、この事故を
    防いでいる。
    """
    if not records:
        logger.warning(
            "upsert_projects_and_sweep: records is empty; スキップします"
            "(意図しない全件削除を避けるため、空リストではミラーを更新しません)"
        )
        return 0

    synced_at = db_truncated_utcnow()
    with _connect() as conn:
        with conn.cursor() as cur:
            for batch in _chunked(records, _UPSERT_BATCH_SIZE):
                _upsert_batch(cur, batch, synced_at=synced_at)
            cur.execute(
                'DELETE FROM "ProjectMirror" WHERE "syncedAt" < %s',
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
                Json(record["data"]),
                record.get("last_edited_at"),
                synced_at,
            ]
        )
    cur.execute(
        f"""
        INSERT INTO "ProjectMirror" (id, "notionPageId", data, "lastEditedAt", "syncedAt")
        VALUES {values_sql}
        ON CONFLICT ("notionPageId") DO UPDATE SET
            data = EXCLUDED.data,
            "lastEditedAt" = EXCLUDED."lastEditedAt",
            "syncedAt" = EXCLUDED."syncedAt"
        """,
        params,
    )


def get_project_count(*, synced_since: datetime | None = None) -> int:
    """`ProjectMirror`の現在の行数。`refresh_all_projects()`が新規取得件数と比較し、
    異常な急減(部分取得によるsweep事故)を検知するために使う(2026-08-18)。

    `synced_since`を渡すと`syncedAt = synced_since`の行数だけを返す(2026-09-01)。
    分割実行(`refresh_projects_incrementally()`)では1回ぶんの取得件数と全体の件数を
    比べても意味が無い(1回は2,000件、全体は26,017件)ため、掃除の直前に
    「**この一巡で触れた行が何件あるか**」を数えて急減を検知するのに使う。
    これは`sweep_projects(before=...)`が消し**残す**行数そのもの。
    """
    with _connect() as conn, conn.cursor() as cur:
        if synced_since is None:
            cur.execute('SELECT count(*) AS n FROM "ProjectMirror"')
        else:
            # **等号で数える**（2026-09-01、ChatGPTのレビュー指摘）。
            # `>=`にすると、一巡の最中にWebhookが`syncedAt = now()`で更新した行まで
            # 「この一巡で触れた」と数えてしまい、**部分取得の検知が鈍る**
            # （取れていないのに件数が足りているように見える）。
            # 一巡の書き込みは必ず`pass_started_at`ちょうど（ミリ秒境界の不動点）なので、
            # 等号なら「この一巡が実際に触れた行」だけを正確に数えられる。
            cur.execute(
                'SELECT count(*) AS n FROM "ProjectMirror" WHERE "syncedAt" = %s',
                (synced_since,),
            )
        return cur.fetchone()["n"]


def list_projects() -> list[dict[str, Any]]:
    """ミラー全件を読み取る。`data`カラムをそのまま`list[dict]`で返す。

    `syncedAt`昇順で返す。この順序自体に意味があるわけではなく、`ORDER BY`を指定しない
    場合の読み取り順序はPostgres側で保証されない(実行計画次第で変わりうる)ため、
    決定的な順序にしておく(呼び出しごとの並び不安定さによるテスト・表示側のノイズ回避)。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('SELECT data FROM "ProjectMirror" ORDER BY "syncedAt"')
        return [row["data"] for row in cur.fetchall()]
