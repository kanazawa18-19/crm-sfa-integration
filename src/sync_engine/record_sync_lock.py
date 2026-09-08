"""同一レコードの同期を直列化し、受理済み時刻をDBに保存する。"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from collections.abc import Iterator
from typing import Any

from psycopg.conninfo import conninfo_to_dict

from src import db_utils
from src.sync_engine.id_mapping import SQLiteIdMappingStore

logger = logging.getLogger(__name__)


class RecordSyncBusy(RuntimeError):
    """別の同期が実行中。Webhookは成功応答せず再送させる。"""


class RecordSyncConfigurationError(RuntimeError):
    """安全な同期用DB接続が用意されていない。"""


def lock_key(db_key: str, notion_key: str) -> int:
    digest = hashlib.blake2b(
        f"record_sync:{db_key}:{notion_key}".encode(), digest_size=8
    ).digest()
    return int.from_bytes(digest, "big", signed=True)


class RecordSyncGuard:
    def __init__(
        self, db_key: str, notion_key: str, *, conn: Any = None,
        state: dict[str, datetime] | None = None,
    ) -> None:
        self.db_key = db_key
        self.notion_key = notion_key
        self.conn = conn
        self.state = state if state is not None else {}
        if conn is not None:
            with conn.cursor() as cur:
                cur.execute(
                    'SELECT "acceptedAt", "completedAt" FROM "RecordSyncWatermark" '
                    'WHERE "dbKey" = %s AND "notionKey" = %s', (db_key, notion_key)
                )
                self.state = cur.fetchone() or {}

    def latest(self, previous: datetime | None) -> datetime | None:
        """mappingと永続化済みの完了時刻のうち、新しい方を返す。"""
        values = [db_utils.ensure_utc(v) for v in (previous, self.state.get('completedAt')) if v is not None]
        return max(values) if values else None

    def rejects(self, occurred_at: datetime) -> bool:
        accepted = self.state.get('acceptedAt')
        return accepted is not None and occurred_at < db_utils.ensure_utc(accepted)

    def accept(self, occurred_at: datetime) -> None:
        self._save(occurred_at, completed=False)

    def advance(self, occurred_at: datetime) -> None:
        """受理時刻と完了時刻を、過去へ戻さず永続化する。"""
        self._save(occurred_at, completed=True)

    def _save(self, occurred_at: datetime, *, completed: bool) -> None:
        if self.conn is not None:
            with self.conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO "RecordSyncWatermark" ("dbKey", "notionKey", "acceptedAt", "completedAt") '
                    'VALUES (%s, %s, %s, %s) ON CONFLICT ("dbKey", "notionKey") DO UPDATE SET '
                    '"acceptedAt" = GREATEST("RecordSyncWatermark"."acceptedAt", EXCLUDED."acceptedAt"), '
                    '"completedAt" = GREATEST("RecordSyncWatermark"."completedAt", EXCLUDED."completedAt")',
                    (self.db_key, self.notion_key, occurred_at, occurred_at if completed else None),
                )
        accepted = self.state.get('acceptedAt')
        self.state['acceptedAt'] = max(occurred_at, db_utils.ensure_utc(accepted)) if accepted else occurred_at
        if completed:
            previous = self.state.get('completedAt')
            self.state['completedAt'] = max(occurred_at, db_utils.ensure_utc(previous)) if previous else occurred_at


@contextmanager
def acquire_record_sync_lock(store: Any, db_key: str, notion_key: str) -> Iterator[RecordSyncGuard]:
    # SQLite実装だけは単一プロセスのローカル検証用。未設定本番の無排他継続は禁止。
    if isinstance(store, SQLiteIdMappingStore):
        with store._sync_lock_registry_guard:
            lock, state = store._sync_locks.setdefault((db_key, notion_key), (threading.Lock(), {}))
        if not lock.acquire(blocking=False):
            raise RecordSyncBusy('同じレコードの同期が実行中です')
        try:
            yield RecordSyncGuard(db_key, notion_key, state=state)
        finally:
            lock.release()
        return

    conn = _connect_direct()
    key = lock_key(db_key, notion_key)
    acquired = False
    try:
        # watermarkは外部書込より先に確定する。ロックはセッション単位で保持。
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(%s) AS locked', (key,))
            acquired = bool(cur.fetchone()['locked'])
        if not acquired:
            raise RecordSyncBusy('同じレコードの同期が実行中です')
        yield RecordSyncGuard(db_key, notion_key, conn=conn)
    finally:
        # 接続を閉じるとロックも解放される。例外時も接続を残さない。
        conn.close()


def _connect_direct() -> Any:
    """環境変数の暗黙ホストを使わず、直接接続の設定を確認する。"""
    url = os.environ.get('DATABASE_URL_UNPOOLED')
    if not url:
        raise RecordSyncConfigurationError('DATABASE_URL_UNPOOLED が必要です')
    try:
        host = conninfo_to_dict(url).get('host', '')
    except Exception:
        raise RecordSyncConfigurationError('同期用DB接続の形式が不正です') from None
    if not host or any(not part for part in host.split(',')):
        raise RecordSyncConfigurationError('同期用DB接続にはホストの明示が必要です')
    if '-pooler' in host.lower():
        raise RecordSyncConfigurationError('同期用DB接続には直接接続が必要です')
    return db_utils.connect_for_advisory_lock(logger)


def validate_record_sync_storage(store: Any) -> None:
    """キューを取得する前に、共有する同期DBの必要条件を読み取りで検査する。"""
    if isinstance(store, SQLiteIdMappingStore):
        return
    conn = _connect_direct()
    try:
        with conn.cursor() as cur:
            # 実際の列を解決し、未配備やスキーマの不一致を先に検出する。
            cur.execute(
                'SELECT "dbKey", "notionKey", "acceptedAt", "completedAt" '
                'FROM "RecordSyncWatermark" LIMIT 0'
            )
            cur.execute(
                "SELECT has_table_privilege(%s, 'SELECT') "
                "AND has_table_privilege(%s, 'INSERT') "
                "AND has_table_privilege(%s, 'UPDATE') "
                "AND current_setting('transaction_read_only') = 'off' AS ready",
                ('"RecordSyncWatermark"',) * 3,
            )
            row = cur.fetchone()
            if not row or not row['ready']:
                raise RecordSyncConfigurationError('同期時刻テーブルの読み書き権限が必要です')
    finally:
        conn.close()
