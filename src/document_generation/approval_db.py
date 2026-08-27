"""DocumentApprovalテーブル(Neon Postgres)への直接アクセス(2026-08-18)。

`src/audit_log/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで読み書きするのみで
マイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を共有する。

見積書の承認リクエスト状態(`status`: "in_progress" | "approved" | "declined" | "cancelled")を
保持する。承認者一覧(DocumentApprover)はdashboard側がPrismaで直接CRUDする
(承認リクエスト送信時にPython側へ渡されるのは選択済みの`approver_emails`のみのため、
Python側からDocumentApproverを読む必要がない)。

複数承認者対応(2026-08-27): `approverEmails`(配列)が正。旧`approverEmail`(単一)カラムは
nullableのまま残っており、insert時に先頭1件をdual-writeするが、これはロールバック時の
経過措置に過ぎない。読み取りは必ず`approverEmails`を使うこと
(docs/quote_approval_note.md参照)。

`_row_to_approval()`は、`prisma migrate deploy`(ビルド時)適用〜新デプロイ公開までの
デプロイ窓で旧コードがINSERTした行(`approverEmails`がNULL・旧`approverEmail`のみ埋まった行)
を読んだ場合、`approverEmail`から1要素配列を組み立てるフォールバックを持つ。dual-writeと
対になる経過措置であり、旧`approverEmail`列を削除する別マイグレーションの際に一緒に削除する。
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
    approver_emails: list[str]
    requested_by_email: str
    status: str
    created_at: datetime
    resolved_at: datetime | None


_COLUMNS = (
    'id, "notionProjectId", category, "driveFileId", "driveApprovalId", "approverEmails", '
    '"approverEmail", "requestedByEmail", status, "createdAt", "resolvedAt"'
)


def _row_to_approval(row: dict[str, Any]) -> DocumentApproval:
    """行を`DocumentApproval`へ変換する。

    複数承認者対応(2026-08-27)のデプロイ窓で作られた行への読み取りフォールバック
    (shirokuma-secレビューBLOCKER対応): `prisma migrate deploy`はビルド時に走るため、
    新デプロイが公開されるまでの数十秒は旧`insert_document_approval()`が動いており、
    その間にINSERTされた行は`approverEmails`がNULL(psycopgではNone)のまま旧`approverEmail`
    (単一)のみ埋まっている。この場合`approverEmails`を「承認者0人」として読んでしまうと
    Slack通知の承認者欄が空になるため、`approverEmail`から1要素配列を組み立てて使う。
    これはdual-write(`insert_document_approval()`が両カラムへ書く経過措置)と対になる
    読み取り側の経過措置であり、旧`approverEmail`列を削除する別マイグレーションの際に
    dual-writeと一緒に削除する(docs/quote_approval_note.md参照)。
    """
    approver_emails = row["approverEmails"] or (
        [row["approverEmail"]] if row["approverEmail"] else []
    )
    return DocumentApproval(
        id=row["id"],
        notion_project_id=row["notionProjectId"],
        category=row["category"],
        drive_file_id=row["driveFileId"],
        drive_approval_id=row["driveApprovalId"],
        approver_emails=approver_emails,
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

    `approverEmail`(旧単一カラム、nullable)にも先頭1件をdual-writeする——
    `approverEmails`が正であり、この書き込みはロールバック時に旧コードが通知文面で
    Noneを出さないための経過措置。安定後に別マイグレーションで`approverEmail`列自体を
    削除する予定(docs/quote_approval_note.md参照)。
    """
    # 明示的な列指定にしているのは、`_COLUMNS`(SELECT向け、読み取りフォールバック用に
    # "approverEmail"を含む)をそのまま使うと"approverEmail"が二重に並んでしまうため
    # (INSERT文は"createdAt"/"resolvedAt"がリテラル(`now()`/`NULL`)である点でもSELECT向けの
    # 並びと異なる)。
    approval_id = uuid.uuid4().hex
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "DocumentApproval"
                (id, "notionProjectId", category, "driveFileId", "driveApprovalId",
                 "approverEmails", "approverEmail", "requestedByEmail", status, "createdAt", "resolvedAt")
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), NULL)
            """,
            (
                approval_id,
                notion_project_id,
                category,
                drive_file_id,
                drive_approval_id,
                approver_emails,
                approver_emails[0],
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
