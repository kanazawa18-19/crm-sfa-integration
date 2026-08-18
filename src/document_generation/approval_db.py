"""DocumentApprovalテーブル(Neon Postgres)への直接アクセス(2026-08-18)。

`src/audit_log/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで読み書きするのみで
マイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を共有する。

見積書の承認リクエスト状態(`status`: "in_progress" | "approved" | "declined" | "cancelled")を
保持する。承認者一覧(DocumentApprover)はdashboard側がPrismaで直接CRUDする
(承認リクエスト送信時にPython側へ渡されるのは選択済みの`approver_email`のみのため、
Python側からDocumentApproverを読む必要がない)。
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

IN_PROGRESS = "in_progress"
APPROVED = "approved"
DECLINED = "declined"
CANCELLED = "cancelled"


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
    approver_email: str
    requested_by_email: str
    status: str
    created_at: datetime
    resolved_at: datetime | None


_COLUMNS = (
    'id, "notionProjectId", category, "driveFileId", "driveApprovalId", "approverEmail", '
    '"requestedByEmail", status, "createdAt", "resolvedAt"'
)


def _row_to_approval(row: dict[str, Any]) -> DocumentApproval:
    return DocumentApproval(
        id=row["id"],
        notion_project_id=row["notionProjectId"],
        category=row["category"],
        drive_file_id=row["driveFileId"],
        drive_approval_id=row["driveApprovalId"],
        approver_email=row["approverEmail"],
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
    approver_email: str,
    requested_by_email: str,
) -> str:
    """承認リクエスト送信直後に1件作成する(status="in_progress")。生成したidを返す。"""
    approval_id = uuid.uuid4().hex
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO "DocumentApproval"
                ({_COLUMNS})
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), NULL)
            """,
            (
                approval_id,
                notion_project_id,
                category,
                drive_file_id,
                drive_approval_id,
                approver_email,
                requested_by_email,
                IN_PROGRESS,
            ),
        )
        conn.commit()
    return approval_id


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
