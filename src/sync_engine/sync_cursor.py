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

from src.db_utils import db_truncated_utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncCursor:
    """一巡の進み具合。"""

    name: str
    #: 最後に取り込んだページの created_time。Noneなら一巡の先頭から。
    watermark: str | None
    #: この一巡を始めた時刻。掃除の基準になる。
    #: **必ずミリ秒境界（マイクロ秒が1000の倍数）に乗っていること。** 理由は
    #: `load_cursor()`のdocstring参照（`TIMESTAMP(3)`の丸めによる誤削除の防止）。
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
    """しおりを読む。無ければ新しい一巡を始める。

    ■ 新しい一巡の開始時刻は必ず`db_truncated_utcnow()`を通す（2026-09-01、レビュー指摘）

    `pass_started_at`は、この一巡で取り込む行の`syncedAt`（`TIMESTAMP(3)`＝ミリ秒精度）
    として**書き込みにも**、一巡し終えたあとの掃除の`WHERE "syncedAt" < 基準時刻`という
    **比較にも**使う。素の`datetime.now(timezone.utc)`（マイクロ秒精度）をそのまま使うと、
    書き込み時はPostgresが四捨五入でミリ秒へ丸める一方、比較には丸められていない元の値が
    使われるため、丸め方向次第で`保存値 < 比較用の元の値`が真になり、
    **今まさに書き込んだ行まで掃除で消える。**

    これは2026-08-25に本番で実際に起きた事故そのもの（`db_truncated_utcnow()`の
    docstring・`docs/project_mirror_activation_note.md`参照）。同じ形の
    mark-and-sweep を新設したのに、基準時刻だけこの保護を通っていなかった。

    一度DBへ保存して読み直した値は`TIMESTAMP(3)`列を経由するので結果的に丸まるが、
    **一巡が1回の実行で完結する場合はDBを経由しない**ため、そこだけ穴になっていた。
    ここで最初から不動点（1000の倍数）にしておけば、経路によらず常に一致する。
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT name, watermark, "passStartedAt" FROM "SyncCursor" WHERE name = %s', (name,)
        )
        row = cur.fetchone()
    if row is None:
        return SyncCursor(name=name, watermark=None, pass_started_at=db_truncated_utcnow())
    started = row["passStartedAt"]
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    # DBから読んだ値も念のためミリ秒境界へ揃える（2026-09-01、Geminiレビュー指摘）。
    # `passStartedAt`は`TIMESTAMP(3)`なので現状は既に揃っているが、
    # **列の精度が変わった瞬間に事故Bが静かに戻ってくる**ので、依存しない形にしておく。
    started = started.replace(microsecond=(started.microsecond // 1000) * 1000)
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
