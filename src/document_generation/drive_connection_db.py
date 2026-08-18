"""RepDriveConnectionテーブル(Neon Postgres)への直接アクセス(2026-08-18)。

`src/gmail_sync/db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで読むのみで
マイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を共有する。

営業担当者個人のDrive OAuth接続(見積書承認リクエストフロー、`RepGmailConnection`と同じ
「個人OAuth同意」方式。経緯は計画書「認証方式の紆余曲折」参照)を読むためのモジュール。
書き込み(接続・解除)はdashboard側のServer Actionが担当するため、ここでは読み取りのみ。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeoutを明示しないとハングしうる(src/gmail_sync/db.pyと同じ理由)。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


@dataclass(frozen=True)
class RepDriveConnection:
    rep_email: str
    refresh_token_enc: str
    connected_at: datetime


def get_rep_drive_connection(rep_email: str) -> RepDriveConnection | None:
    """`rep_email`のDrive OAuth接続を1件取得する。未接続ならNoneを返す
    (呼び出し元の`quote_generator.request_quote_approval()`が
    「先にDrive連携を接続してください」エラーへ変換する)。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "repEmail", "refreshTokenEnc", "connectedAt" '
            'FROM "RepDriveConnection" WHERE "repEmail" = %s',
            (rep_email,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return RepDriveConnection(
        rep_email=row["repEmail"],
        refresh_token_enc=row["refreshTokenEnc"],
        connected_at=row["connectedAt"],
    )
