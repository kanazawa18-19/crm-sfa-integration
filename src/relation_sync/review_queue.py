"""RelationReviewQueueテーブル(Neon Postgres)への直接アクセス(2026-08-25)。

`src/audit_log/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで書き込むのみで
マイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を共有する。

リレーション解決(`src/relation_sync/resolve.py`)が自動確定できなかった(候補0件・複数件)
ケースを、`src/migration/`の`needs_review_clients`（移行時、CSV+コンソール出力していた
同種の判断）と同じ考え方でリアルタイム処理向けに永続化する。resolved/dismissedへの遷移は
今回のスコープでは扱わず、手動SQL操作を前提とする（一覧取得の`list_pending_reviews()`のみ
用意する）。

■ 重複防止について（shirokuma-sec/obasan-qualityレビューWARN対応、2026-08-25）: 同一の
(`sourceTool`, `sourceRecordId`, `targetDbKey`, `rawValue`)の組み合わせでpending状態の行が
既に存在する場合は再挿入しない。当初は「SELECT存在確認→INSERT」の2クエリで実装していたが、
同一トランザクション内でのロックも一意制約も無くほぼ同時に同一内容のWebhookが複数走った
場合に競合状態で重複INSERTされうる不具合があったため、Postgresの部分ユニークインデックス
（`dashboard/prisma/migrations/20260825060000_add_relation_sync/migration.sql`の
`RelationReviewQueue_pending_dedupe_key`、`status = 'pending'`の行のみを対象とする）と
`INSERT ... ON CONFLICT ... DO NOTHING`によるDB側での原子的な重複排除に変更した
（`status`列の値によって重複防止の対象が変わる特性上、Prismaのスキーマ定義言語では部分
インデックスを表現できないため、このインデックスは`schema.prisma`上の`@@unique`/`@@index`
ではなくmigration.sqlにのみ存在する。Prismaの制約についてはschema.prisma側のコメントも
参照）。
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

logger = logging.getLogger(__name__)


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeoutを明示しないとハングしうる(src/project_mirror/db.pyと同じ理由)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def enqueue_for_review(
    *,
    source_tool: str,
    source_record_id: str,
    target_db_key: str,
    raw_value: str,
    candidate_notion_page_ids: list[str],
    candidate_raw_names: list[str],
) -> None:
    """リレーション解決が曖昧だった1件をRelationReviewQueueへ記録する(pending状態)。

    kintone Webhook等は同一レコードへの編集のたびに何度も呼ばれうるため、同一の
    (`source_tool`, `source_record_id`, `target_db_key`, `raw_value`)の組み合わせで既に
    pending状態のキューが存在する場合は再度挿入しない(この関数を素朴に毎回INSERTする実装
    にすると、解決できないまま放置された1レコードが編集されるたびにキューへ同じ内容の行が
    積み上がり、レビューキューとしての「確認すべき件数」の意味が失われるため)。

    重複防止は`RelationReviewQueue_pending_dedupe_key`（`status = 'pending'`の行のみを対象と
    する部分ユニークインデックス）への`ON CONFLICT ... DO NOTHING`で行う（モジュール
    docstring参照）。SELECTしてからINSERTする2クエリの実装ではなく1クエリでDB側が原子的に
    処理するため、同時に複数のWebhookが同一内容で呼ばれても競合状態にならない。

    `candidate_raw_names`は`candidate_notion_page_ids`と同じ順序・同じ件数で対になる各候補の
    取引先名一覧（`src/relation_sync/db.py`の`find_by_normalized_name()`が返す`raw_name`を
    そのまま使う）。これを保存せずpage IDのみ記録すると、`scripts/list_relation_review_queue.py`
    での確認時に運用者が毎回Notionを開いてpage IDを1件ずつ引かないと候補の実体（会社名）が
    分からず、このスクリプト本来の目的（機械的な確認を省く）を果たせない
    （shirokuma-sec/obasan-qualityレビューWARN対応、2026-08-25）。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "RelationReviewQueue"
                (id, "sourceTool", "sourceRecordId", "targetDbKey", "rawValue",
                 "candidateNotionPageIds", "candidateRawNames")
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT ("sourceTool", "sourceRecordId", "targetDbKey", "rawValue")
                WHERE status = 'pending'
            DO NOTHING
            """,
            (
                uuid.uuid4().hex,
                source_tool,
                source_record_id,
                target_db_key,
                raw_value,
                Json(candidate_notion_page_ids),
                candidate_raw_names,
            ),
        )
        if cur.rowcount == 0:
            logger.info(
                "enqueue_for_review: 同一内容のpendingエントリが既に存在するためスキップしました"
                " (source_tool=%r, source_record_id=%r, target_db_key=%r)",
                source_tool,
                source_record_id,
                target_db_key,
            )
        conn.commit()


def list_pending_reviews() -> list[dict[str, Any]]:
    """status="pending"のRelationReviewQueueを作成日時の古い順に返す。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT "id", "sourceTool", "sourceRecordId", "targetDbKey", "rawValue",
                   "candidateNotionPageIds", "candidateRawNames", "createdAt"
            FROM "RelationReviewQueue"
            WHERE status = 'pending'
            ORDER BY "createdAt"
            """
        )
        return cur.fetchall()
