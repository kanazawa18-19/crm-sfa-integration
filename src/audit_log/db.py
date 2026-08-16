"""AuditLogテーブル(Neon Postgres)への直接アクセス(2026-08-17)。

`src/gmail_sync/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで書き込むのみで
マイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を共有する。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeoutを明示しないとハングしうる(src/gmail_sync/db.pyと同じ理由)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def insert_audit_log(
    *,
    db_key: str,
    notion_page_id: str,
    action: str,
    changed_fields: dict[str, Any],
    actor_source: str,
    actor_label: str | None,
) -> None:
    """`AuditLog`へ1件挿入する。呼び出し元(`recorder.py`)が例外を握りつぶす設計のため、
    ここでは特にリトライ等は行わない。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "AuditLog"
                (id, "dbKey", "notionPageId", action, "changedFields",
                 "actorSource", "actorLabel", "createdAt")
            VALUES (%s, %s, %s, %s, %s, %s, %s, now())
            """,
            (
                uuid.uuid4().hex,
                db_key,
                notion_page_id,
                action,
                Json(changed_fields),
                actor_source,
                actor_label,
            ),
        )
        conn.commit()
