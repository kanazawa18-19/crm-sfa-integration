"""DocumentApprovalテーブル(Neon Postgres)への直接アクセス(2026-08-18)。

`src/audit_log/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで読み書きするのみで
マイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を共有する。

見積書の承認リクエスト状態(`status`: "in_progress" | "approved" | "declined" | "cancelled")を
保持する。承認者一覧(DocumentApprover)はdashboard側がPrismaで直接CRUDする
(承認リクエスト送信時にPython側へ渡されるのは選択済みの`approver_emails`のみのため、
Python側からDocumentApproverを読む必要がない)。

複数承認者対応(2026-08-27): `approverEmails`(配列)が正。

**経過措置は2026-08-28に撤去済み**: 旧`approverEmail`(単一)カラムへのdual-writeと、
`approverEmails`がNULLの行を旧カラムから読む`_row_to_approval()`のフォールバックは、
マイグレーション`20260828000000_document_approval_approver_emails_not_null`で
バックフィル＋NOT NULL化したことで不要になったため削除した。このモジュールは
旧`approverEmail`列を一切読み書きしない。

**列そのもののDROPはまだ**。このコードが本番稼働してから次のマイグレーションで行う
(同時にDROPすると、ビルド時マイグレーション〜新デプロイ公開までの数十秒に動いている
1つ前のコードが、まだ`approverEmail`をSELECTしていて500になるため。
docs/quote_approval_note.md参照)。
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src import db_utils

logger = logging.getLogger(__name__)

IN_PROGRESS = "in_progress"
APPROVED = "approved"
DECLINED = "declined"
CANCELLED = "cancelled"

# `try_acquire_approval_lock()`/`release_approval_lock()`が使うPostgresアドバイザリロックの
# 名前空間(int4)。`src/project_mirror/db.py`・`src/relation_sync/db.py`の
# `_REFRESH_LOCK_KEY`(いずれも引数1個のbigint版`pg_try_advisory_lock(bigint)`、固定キー)とは
# 異なり、こちらは案件・カテゴリごとに異なるキーで取り合う必要があるため、引数2個の
# `pg_try_advisory_lock(int, int)`版を使う(第1引数を用途の名前空間、第2引数を
# `hashtext(notion_project_id || category)`にする、TOCTOU対策、2026-08-28)。値そのものに
# 意味は無く、他用途の名前空間(917_263_540/917_263_541)と衝突しなければよい。
_APPROVAL_LOCK_NAMESPACE = 917_263_542


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


@dataclass(frozen=True)
class DocumentApproval:
    id: str
    notion_project_id: str
    category: str
    drive_file_id: str
    drive_approval_id: str
    approver_emails: list[str]
    requested_by_email: str
    status: str
    created_at: datetime
    resolved_at: datetime | None


_COLUMNS = (
    'id, "notionProjectId", category, "driveFileId", "driveApprovalId", "approverEmails", '
    '"requestedByEmail", status, "createdAt", "resolvedAt"'
)


def _row_to_approval(row: dict[str, Any]) -> DocumentApproval:
    """行を`DocumentApproval`へ変換する。

    複数承認者対応(2026-08-27)のデプロイ窓で作られた「`approverEmails`がNULLで旧
    `approverEmail`のみ埋まった行」への読み取りフォールバックは、2026-08-28の
    マイグレーション(`20260828000000_document_approval_approver_emails_not_null`)で
    **バックフィル＋NOT NULL化したため不要になり削除した**。この列にNULLは存在し得ない。
    """
    return DocumentApproval(
        id=row["id"],
        notion_project_id=row["notionProjectId"],
        category=row["category"],
        drive_file_id=row["driveFileId"],
        drive_approval_id=row["driveApprovalId"],
        approver_emails=row["approverEmails"],
        requested_by_email=row["requestedByEmail"],
        status=row["status"],
        created_at=row["createdAt"],
        resolved_at=row["resolvedAt"],
    )


def insert_document_approval(
    *,
    notion_project_id: str,
    category: str,
    drive_file_id: str,
    drive_approval_id: str,
    approver_emails: list[str],
    requested_by_email: str,
) -> str:
    """承認リクエスト送信直後に1件作成する(status="in_progress")。生成したidを返す。

    旧`approverEmail`(単一カラム)へのdual-writeは2026-08-28に削除した。ロールバック時に
    旧コードが通知文面でNoneを出さないための経過措置だったが、いま本番で動いているのは
    `approverEmails`を読むコードであり、経過措置の役目は終わっている。列自体の削除は
    「この列を読み書きしないコードが本番稼働してから」次のマイグレーションで行う
    (docs/quote_approval_note.md参照)。
    """
    # 明示的な列指定にしているのは、INSERT文は"createdAt"/"resolvedAt"がリテラル
    # (`now()`/`NULL`)であり、SELECT向けの`_COLUMNS`とは並びが異なるため。
    approval_id = uuid.uuid4().hex
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "DocumentApproval"
                (id, "notionProjectId", category, "driveFileId", "driveApprovalId",
                 "approverEmails", "requestedByEmail", status, "createdAt", "resolvedAt")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), NULL)
            """,
            (
                approval_id,
                notion_project_id,
                category,
                drive_file_id,
                drive_approval_id,
                approver_emails,
                requested_by_email,
                IN_PROGRESS,
            ),
        )
        conn.commit()
    return approval_id


def try_acquire_approval_lock(
    notion_project_id: str, category: str
) -> psycopg.Connection[dict[str, Any]] | None:
    """`request_quote_approval()`の「重複チェック→送信→INSERT」区間の多重実行防止用に
    `pg_try_advisory_lock(int, int)`を試みる(`src/project_mirror/db.py`の
    `try_acquire_refresh_lock()`と同じ設計・作法。TOCTOU対策、2026-08-28)。

    同じ`notion_project_id`・`category`の組で既に別のリクエストが処理中の場合は`None`を返す
    (非ブロッキング。ボタン連打や別ウィンドウからのほぼ同時送信を「待たせて後で通す」のではなく
    即座に失敗させ、呼び出し元(`quote_generator.request_quote_approval()`)が
    `DuplicateApprovalRequestError`へ変換する)。

    取得できた場合はロックを保持したままの`Connection`を返す(セッション単位のロックのため、
    呼び出し元は処理完了後に必ず`release_approval_lock()`でこの接続ごと解放すること)。

    `cur.execute()`が例外を投げた場合も接続をcloseしてから再送出する(呼び出し元はまだ
    `Connection`を受け取っていないため、ここでcloseしないと接続がリークする。
    `src/project_mirror/db.py`の`try_acquire_refresh_lock()`と同じ形で存在した既存の
    リークパターンで、両方まとめて修正した。QAレビューWARN対応、2026-08-28)。

    通常のCRUD用の`_connect()`とは異なり、`db_utils.connect_for_advisory_lock()`
    (`DATABASE_URL_UNPOOLED`優先)を使う。理由は`docs/quote_approval_note.md`の
    「前提条件: advisory lockは非pooled接続でのみ機能する」参照(2026-08-28)。
    """
    conn = db_utils.connect_for_advisory_lock(logger)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(%s, hashtext(%s)) AS locked",
                (_APPROVAL_LOCK_NAMESPACE, f"{notion_project_id}:{category}"),
            )
            row = cur.fetchone()
    except Exception:
        conn.close()
        raise
    if not (row and row["locked"]):
        conn.close()
        return None
    return conn


def release_approval_lock(
    conn: psycopg.Connection[dict[str, Any]], notion_project_id: str, category: str
) -> None:
    """`try_acquire_approval_lock()`で取得したロックを解放し、接続を閉じる。

    `notion_project_id`・`category`は`try_acquire_approval_lock()`呼び出し時と同じ値を渡す
    こと(`pg_advisory_unlock(int, int)`はロック取得時と同じキーの組で呼ぶ必要がある)。
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(%s, hashtext(%s))",
                (_APPROVAL_LOCK_NAMESPACE, f"{notion_project_id}:{category}"),
            )
    finally:
        conn.close()


def find_in_progress_approval(notion_project_id: str, category: str) -> DocumentApproval | None:
    """同じ案件・カテゴリで既に`in_progress`の承認リクエストが無いか確認する
    (承認リクエスト送信時の重複送信防止、2026-08-18)。あれば1件返す。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f'SELECT {_COLUMNS} FROM "DocumentApproval" '
            'WHERE "notionProjectId" = %s AND category = %s AND status = %s '
            "LIMIT 1",
            (notion_project_id, category, IN_PROGRESS),
        )
        row = cur.fetchone()
    return _row_to_approval(row) if row is not None else None


def list_in_progress_approvals() -> list[DocumentApproval]:
    """`status = "in_progress"`の承認リクエストを全件返す(承認状態ポーリングcron向け)。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f'SELECT {_COLUMNS} FROM "DocumentApproval" WHERE status = %s', (IN_PROGRESS,))
        rows = cur.fetchall()
    return [_row_to_approval(row) for row in rows]


def update_approval_status(approval_id: str, status: str) -> None:
    """承認状態が確定した(`APPROVED`/`DECLINED`/`CANCELLED`)ものを反映し、`resolvedAt`を
    現在時刻で埋める。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'UPDATE "DocumentApproval" SET status = %s, "resolvedAt" = now() WHERE id = %s',
            (status, approval_id),
        )
        conn.commit()
