"""スプレッドシートの行を新規作成するときのレコード単位の排他（2026-08-31）。

**なぜ必要か。**
「同期キーで探す → 無ければ追記する」の間に、別のワーカーが同じレコードの行を作ると
2行できる。同期キーで引き直す仕組みを入れたことで窓は大きく狭まったが、
探すと追記の間そのものは残る（Gemini・ChatGPTの両方から指摘）。

FastAPIのワーカーが複数動く本番では現実的に踏むため、**行を作る瞬間だけ**
レコード単位のadvisory lockを取る。既に行がある場合（＝更新）はロックを取らない。
1レコードにつき生涯1回しか通らない経路なので、常時の負荷にはならない。

**取れなかったら追記しない。** 別のワーカーがまさに作っている最中なので、
そこで追記すると重複になる。書き込みを1回見送っても、次の同期イベントで
同期キーから引けるようになるため、データが失われることはない。

ロック用の接続は`db_utils.connect_for_advisory_lock()`（`DATABASE_URL_UNPOOLED`優先）を使う。
Neonのpooled接続ではセッション単位のadvisory lockが**例外も出さずに無効化される**ため。
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from src import db_utils

logger = logging.getLogger(__name__)

#: 他のadvisory lockとキー空間が衝突しないよう、用途ごとに固定の接頭辞を混ぜる。
_LOCK_NAMESPACE = "spreadsheet_row_creation"

_missing_database_url_warned = False


def lock_key(db_key: str, notion_key: str) -> int:
    """レコードを一意に表す64bit整数（`pg_try_advisory_lock`の引数）。

    `db_key`も混ぜるのは、DBが違えば別レコードだから
    （Notionキーは接頭辞で分かれているが、前提にしない）。
    """
    digest = hashlib.blake2b(
        f"{_LOCK_NAMESPACE}:{db_key}:{notion_key}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


@contextmanager
def acquire_row_creation_lock(db_key: str, notion_key: str) -> Iterator[bool]:
    """行の新規作成を1レコード1プロセスに限る。取得できたかを`yield`する。

    `False`が返ったら**追記してはいけない**。別のワーカーが作成中である。

    DBが設定されていない環境（ローカル・テスト）では常に`True`を返す。
    その場合は多重実行が起こりえないか、起きても問題にならない前提。
    """
    global _missing_database_url_warned

    if not os.environ.get("DATABASE_URL") and not os.environ.get("DATABASE_URL_UNPOOLED"):
        if not _missing_database_url_warned:
            logger.warning(
                "spreadsheet row creation lock: DATABASE_URLが未設定のため排他せずに続行します。"
                "本番でこの警告が出ている場合、並行するワーカーが同じレコードの行を"
                "重複して作る可能性があります"
            )
            _missing_database_url_warned = True
        yield True
        return

    key = lock_key(db_key, notion_key)
    conn = db_utils.connect_for_advisory_lock(logger)
    acquired = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS locked", (key,))
            row = cur.fetchone()
        acquired = bool(row and row["locked"])
        if not acquired:
            logger.info(
                "spreadsheet row creation lock: 別のワーカーが作成中のため、"
                "この回の追記は見送ります (db_key=%r, notion_key=%r)",
                db_key,
                notion_key,
            )
        yield acquired
    finally:
        try:
            if acquired:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
        finally:
            conn.close()


def reset_missing_database_url_warning() -> None:
    """テスト用。警告は1プロセスに1回だけ出す作りのため、テスト間でリセットする。"""
    global _missing_database_url_warned
    _missing_database_url_warned = False
