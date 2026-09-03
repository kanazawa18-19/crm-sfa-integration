"""`ContactMailPreference`テーブル（Neon Postgres）の読み取り（2026-09-03）。

スキーマ管理はdashboard側のPrisma（`dashboard/prisma/schema.prisma`）に一本化して
おり、ここではraw SQLで読むだけ（`src/gmail_sync/db.py`と同じ方針。同一DBに対する
二重のマイグレーション履歴を作らない）。

**書き込みはここにはない。** 配信停止の登録は、お客様が開く公開ページ
（`dashboard/app/unsubscribe/`）からPrisma経由で行う。読む側と書く側が分かれている
のは、書き込みが「本人の操作」でしか起きないことをコードの形で示すため。
"""

from __future__ import annotations

import os
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

from src.bulk_email.ids import normalize_page_id


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    # connect_timeout / timezone=UTC を明示する理由は`src/gmail_sync/db.py`と同じ。
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def fetch_opt_outs(
    page_ids: Iterable[str], emails: Iterable[str]
) -> tuple[set[str], set[str]]:
    """候補の中から、配信停止の申し出がある連絡先ページIDとメールアドレスを返す。

    テーブル全件ではなく候補で絞るのは、宛先が数十件でもテーブルが数千行に育ちうるため
    （全件読みは行数の伸びに気づけないまま遅くなる）。

    **失敗しても空集合を返さない。** 例外はそのまま呼び出し元へ投げる。
    ここで握り潰すと「配信停止の人が0人」として扱われ、止めた相手に送ってしまう。
    """
    # DBの`contactPageId`は正規化済みの形（ハイフン無し・小文字）で保存されている
    # （公開ページがURLのcパラメータをそのまま入れるため）。一方Notionから来る
    # ページIDはハイフン付き。**照合は正規化した形で行い、返すのは呼び出し元が
    # 持っている元の形**にする（呼び出し元は元の形で除外判定をするため）。
    by_normalized: dict[str, str] = {}
    for page_id in page_ids:
        normalized = normalize_page_id(page_id)
        if normalized:
            by_normalized.setdefault(normalized, page_id)
    addresses = sorted({email.strip().lower() for email in emails if (email or "").strip()})
    if not by_normalized and not addresses:
        return set(), set()

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "contactPageId", "contactEmail" FROM "ContactMailPreference" '
            'WHERE "unsubscribed" = TRUE '
            'AND ("contactPageId" = ANY(%s) OR lower("contactEmail") = ANY(%s))',
            (sorted(by_normalized), addresses),
        )
        rows = cur.fetchall()

    opted_out_ids = {
        by_normalized[normalize_page_id(row["contactPageId"])]
        for row in rows
        if normalize_page_id(row["contactPageId"] or "") in by_normalized
    }
    opted_out_emails = {
        (row["contactEmail"] or "").strip().lower() for row in rows if row["contactEmail"]
    }
    opted_out_emails.discard("")
    return opted_out_ids, opted_out_emails
