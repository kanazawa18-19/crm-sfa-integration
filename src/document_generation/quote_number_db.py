"""QuoteNumberSequenceテーブル(Neon Postgres)への直接アクセス(2026-08-19)。

`src/document_generation/approval_db.py`と同じ方針: このDBのスキーマ管理はdashboard(Next.js)側の
Prisma(dashboard/prisma/schema.prisma)に一本化しており、ここではraw SQLで読み書きするのみで
マイグレーションは行わない。接続文字列はdashboard側と同じDATABASE_URL環境変数を共有する。

見積書NOの正式採番ルール（`CN{YYYYMMDD}{作成者頭文字1字}{当日発行連番2桁}`、例:
`CN20260819K01`）のうち「当日発行連番」を、日付ごとに1から採番するためのカウンタを保持する。
同時に複数の見積書が生成されても連番が重複しないよう、`INSERT ... ON CONFLICT DO UPDATE
... RETURNING`で1クエリ内に原子的にインクリメントする（PostgresはON CONFLICT DO UPDATE時に
対象行のロックを取るため、SELECTしてから別クエリでUPDATEする方式のような競合状態が生じない）。
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


def next_sequence_for_date(date_prefix: str) -> int:
    """`date_prefix`（"YYYYMMDD"形式）の当日発行連番を1つ払い出す（1始まり）。

    同じ`date_prefix`への初回呼び出しは1、以降は呼ぶたびに+1される。日付が変われば
    新しい行が作られ、再び1から始まる。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "QuoteNumberSequence" ("datePrefix", "lastSeq")
            VALUES (%s, 1)
            ON CONFLICT ("datePrefix")
            DO UPDATE SET "lastSeq" = "QuoteNumberSequence"."lastSeq" + 1
            RETURNING "lastSeq"
            """,
            (date_prefix,),
        )
        row = cur.fetchone()
        conn.commit()
    if row is None:
        # `INSERT ... ON CONFLICT ... RETURNING`は常に1行返るはずで、ここに到達するのは
        # 想定外の異常系のみ。`assert`は`-O`最適化フラグ付き実行では無効化されるため、
        # 採番ロジックの不変条件チェックとして明示的にraiseする(shirokuma-secレビューINFO対応)。
        raise RuntimeError(f"QuoteNumberSequence upsert returned no row for date_prefix={date_prefix!r}")
    return row["lastSeq"]
