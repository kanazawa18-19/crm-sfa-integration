"""DocumentApproverテーブル(Neon Postgres)への読み取り専用アクセス(2026-08-18)。

`src/document_generation/drive_connection_db.py`と同じ方針: このDBのスキーマ管理は
dashboard(Next.js)側のPrisma(dashboard/prisma/schema.prisma)に一本化しており、ここでは
raw SQLで読むのみでマイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL
環境変数を共有する。

`POST /api/documents/quote/request-approval`のフロントセレクトボックスは
`DocumentApprover`一覧からしか選べないが、それはUI制約に過ぎずAPIを直接叩けば任意の
メールアドレスを送信できてしまう。承認リクエストを送信する前に、指定された
`approver_email`が実際に登録済みの承認者(`active=true`)かをサーバー側で検証するために使う
(shirokuma-secレビューBLOCKER対応: 未登録・社外の任意メールアドレスへ本物のDrive承認
リクエストが送信できてしまう問題)。
"""

from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def is_active_document_approver(email: str) -> bool:
    """`email`が`DocumentApprover`テーブルに`active=true`で登録済みかどうかを返す。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT 1 FROM "DocumentApprover" WHERE email = %s AND active = true',
            (email,),
        )
        row = cur.fetchone()
    return row is not None
