"""src/project_mirror/db.py（ProjectMirrorテーブルへの直接アクセス）の検証。

実際のPostgresには接続しない。`psycopg.connect`をフェイクの接続/カーソルへ差し替えて、
発行されるSQL・パラメータを検証する（`src/audit_log/db.py`と同じraw SQLパターンのため、
`_connect()`相当をモック化する方針は本テストが初出）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.project_mirror import db


class _FakeCursor:
    def __init__(
        self,
        fetch_rows: list[dict[str, Any]] | None = None,
        fetch_one_rows: list[dict[str, Any] | None] | None = None,
        rowcount: int = 0,
    ) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | list[Any] | None]] = []
        self._fetch_rows = fetch_rows or []
        # fetchone()は呼ばれるたびにこのリストから1件ずつpopする（try_acquire_refresh_lockの
        # ようにSELECTを複数回発行するテストでも呼び出し順に対応する値を返せるようにするため）。
        self._fetch_one_rows = list(fetch_one_rows or [])
        self.rowcount = rowcount

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self._fetch_rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._fetch_one_rows.pop(0) if self._fetch_one_rows else None

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


@pytest.fixture
def fake_cursor() -> _FakeCursor:
    return _FakeCursor()


@pytest.fixture(autouse=True)
def _set_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")


def _patch_connect(monkeypatch: pytest.MonkeyPatch, cursor: _FakeCursor) -> _FakeConnection:
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(db.psycopg, "connect", lambda *args, **kwargs: conn)
    return conn


# --- _connect --------------------------------------------------------------------------------


def test_connect_raises_when_database_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        db._connect()


# --- upsert_project ----------------------------------------------------------------------------


def test_upsert_project_executes_insert_with_on_conflict_and_commits(
    monkeypatch: pytest.MonkeyPatch, fake_cursor: _FakeCursor
) -> None:
    conn = _patch_connect(monkeypatch, fake_cursor)

    db.upsert_project(
        {
            "notion_page_id": "page-1",
            "data": {"案件名": "MSA-PJ-001"},
            "last_edited_at": datetime(2026, 8, 17, tzinfo=timezone.utc),
        }
    )

    assert len(fake_cursor.executed) == 1
    sql, params = fake_cursor.executed[0]
    assert 'INSERT INTO "ProjectMirror"' in sql
    assert "ON CONFLICT" in sql
    assert params[1] == "page-1"
    assert params[2].obj == {"案件名": "MSA-PJ-001"}
    assert params[3] == datetime(2026, 8, 17, tzinfo=timezone.utc)
    assert conn.committed is True


def test_upsert_project_accepts_none_last_edited_at(
    monkeypatch: pytest.MonkeyPatch, fake_cursor: _FakeCursor
) -> None:
    _patch_connect(monkeypatch, fake_cursor)

    db.upsert_project({"notion_page_id": "page-1", "data": {}})

    _, params = fake_cursor.executed[0]
    assert params[3] is None


# --- upsert_projects_and_sweep ------------------------------------------------------------------


def test_upsert_projects_and_sweep_skips_when_records_empty(
    monkeypatch: pytest.MonkeyPatch, fake_cursor: _FakeCursor
) -> None:
    """空リストでのsweepはミラー全件削除の事故になりうるため、何も実行しないこと。"""
    _patch_connect(monkeypatch, fake_cursor)

    result = db.upsert_projects_and_sweep([])

    assert fake_cursor.executed == []
    assert result == 0


def test_upsert_projects_and_sweep_batches_upserts_and_deletes_stale_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cursor = _FakeCursor(rowcount=3)
    conn = _patch_connect(monkeypatch, fake_cursor)
    monkeypatch.setattr(db, "_UPSERT_BATCH_SIZE", 2)

    records = [
        {"notion_page_id": f"page-{i}", "data": {"i": i}, "last_edited_at": None}
        for i in range(5)
    ]

    result = db.upsert_projects_and_sweep(records)

    # 5件をバッチサイズ2で分割すると3バッチ(2,2,1) + 末尾のDELETEで計4クエリ。
    assert len(fake_cursor.executed) == 4
    upsert_calls = fake_cursor.executed[:3]
    delete_call = fake_cursor.executed[3]

    assert "VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)" in upsert_calls[0][0]
    assert len(upsert_calls[0][1]) == 2 * 5  # 2行 x 5カラム
    assert len(upsert_calls[2][1]) == 1 * 5  # 端数バッチは1行分のみ

    delete_sql, delete_params = delete_call
    assert 'DELETE FROM "ProjectMirror"' in delete_sql
    assert 'WHERE "syncedAt" < %s' in delete_sql
    assert len(delete_params) == 1

    assert conn.committed is True
    # DELETEのcur.rowcountがそのまま削除件数として返ること
    # (obasan-qualityレビューWARN対応、2026-08-17。backfillスクリプトの出力に使う)。
    assert result == 3


# --- try_acquire_refresh_lock / release_refresh_lock ---------------------------------------
# shirokuma-secレビューWARN対応(2026-08-17): refresh_all_projects()の多重実行防止用。


def test_try_acquire_refresh_lock_returns_connection_when_lock_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cursor = _FakeCursor(fetch_one_rows=[{"locked": True}])
    conn = _patch_connect(monkeypatch, fake_cursor)

    result = db.try_acquire_refresh_lock()

    assert result is conn
    assert conn.closed is False
    sql, params = fake_cursor.executed[0]
    assert "pg_try_advisory_lock" in sql
    assert params == (db._REFRESH_LOCK_KEY,)


def test_try_acquire_refresh_lock_returns_none_and_closes_connection_when_lock_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cursor = _FakeCursor(fetch_one_rows=[{"locked": False}])
    conn = _patch_connect(monkeypatch, fake_cursor)

    result = db.try_acquire_refresh_lock()

    assert result is None
    assert conn.closed is True


def test_release_refresh_lock_unlocks_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch, fake_cursor: _FakeCursor
) -> None:
    conn = _patch_connect(monkeypatch, fake_cursor)

    db.release_refresh_lock(conn)

    sql, params = fake_cursor.executed[0]
    assert "pg_advisory_unlock" in sql
    assert params == (db._REFRESH_LOCK_KEY,)
    assert conn.closed is True


# --- list_projects -------------------------------------------------------------------------


def test_list_projects_returns_data_column_values(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(
        fetch_rows=[{"data": {"案件名": "A"}}, {"data": {"案件名": "B"}}]
    )
    _patch_connect(monkeypatch, cursor)

    result = db.list_projects()

    assert result == [{"案件名": "A"}, {"案件名": "B"}]


# --- get_project_count ----------------------------------------------------------------------


def test_get_project_count_returns_row_count(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetch_one_rows=[{"n": 10000}])
    _patch_connect(monkeypatch, cursor)

    result = db.get_project_count()

    assert result == 10000
