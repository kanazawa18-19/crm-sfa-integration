"""全件同期の「どこまで進んだか」を覚えておく（2026-09-01）。

■ なぜ要るのか

Notionの取引先マスターは**102,799件**あり、全件取得だけで**約18分**かかる。
Vercelの実行上限は300秒なので、1回の実行では終わらない。
**「1万件で静かに切れる」を直したら、今度は「時間切れで何もしない」になる。**

そこで、時間予算で区切って中断し、次の実行が続きから再開できるようにする。
一巡し終えたときだけ掃除（sweep）を行う。

■ 一巡の途中で掃除してはいけない

掃除は「今回見なかった行を消す」やり方（mark-and-sweep）。途中で掃除すると
**まだ見ていないだけの行を消してしまう。** 実際にProjectMirrorを全消失させた
事故がある（[[project_postgres_mirror]]）。そのため「一巡を始めた時刻」を
覚えておき、**一巡し終えたときだけ**それより古い行を消す。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncCursor:
    """一巡の進み具合。"""

    name: str
    #: 最後に取り込んだページの created_time。Noneなら一巡の先頭から。
    watermark: str | None
    #: この一巡を始めた時刻。掃除の基準になる。
    pass_started_at: datetime

    @property
    def is_new_pass(self) -> bool:
        return self.watermark is None


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10, options="-c timezone=UTC")


def load_cursor(name: str) -> SyncCursor:
    """しおりを読む。無ければ新しい一巡を始める。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT name, watermark, "passStartedAt" FROM "SyncCursor" WHERE name = %s', (name,)
        )
        row = cur.fetchone()
    if row is None:
        return SyncCursor(name=name, watermark=None, pass_started_at=datetime.now(timezone.utc))
    started = row["passStartedAt"]
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return SyncCursor(name=name, watermark=row["watermark"], pass_started_at=started)


def save_cursor(cursor: SyncCursor) -> None:
    """途中まで進んだことを記録する（次回はここから続ける）。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO "SyncCursor" (name, watermark, "passStartedAt", "updatedAt")
            VALUES (%s, %s, %s, now())
            ON CONFLICT (name) DO UPDATE
            SET watermark = EXCLUDED.watermark,
                "passStartedAt" = EXCLUDED."passStartedAt",
                "updatedAt" = now()
            """,
            (cursor.name, cursor.watermark, cursor.pass_started_at),
        )
        conn.commit()


def clear_cursor(name: str) -> None:
    """一巡し終えたので、しおりを捨てる（次回は先頭から）。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('DELETE FROM "SyncCursor" WHERE name = %s', (name,))
        conn.commit()
