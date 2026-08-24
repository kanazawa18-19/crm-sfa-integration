"""src/relation_sync/db.py（ClientNameIndexテーブルへの直接アクセス）の検証。

実際のPostgresには接続しない。`psycopg.connect`をフェイクの接続/カーソルへ差し替えて、
発行されるSQL・パラメータを検証する（tests/project_mirror/test_db.pyと同じパターン）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.relation_sync import db


class _FakeCursor:
    def __init__(
        self,
        fetch_rows: list[dict[str, Any]] | None = None,
        fetch_one_rows: list[dict[str, Any] | None] | None = None,
        rowcount: int = 0,
    ) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | list[Any] | None]] = []
        self._fetch_rows = fetch_rows or []
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


def test_connect_raises_when_database_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        db._connect()


# --- upsert_client_name ----------------------------------------------------------------------


def test_upsert_client_name_executes_insert_with_on_conflict_and_commits(
    monkeypatch: pytest.MonkeyPatch, fake_cursor: _FakeCursor
) -> None:
    conn = _patch_connect(monkeypatch, fake_cursor)

    db.upsert_client_name(
        {
            "notion_page_id": "page-1",
            "normalized_name": "テスト商事",
            "raw_name": "テスト商事株式会社",
        }
    )

    assert len(fake_cursor.executed) == 1
    sql, params = fake_cursor.executed[0]
    assert 'INSERT INTO "ClientNameIndex"' in sql
    assert "ON CONFLICT" in sql
    assert params[1] == "page-1"
    assert params[2] == "テスト商事"
    assert params[3] == "テスト商事株式会社"
    assert conn.committed is True


# --- upsert_client_names_and_sweep ------------------------------------------------------------


def test_upsert_client_names_and_sweep_skips_when_records_empty(
    monkeypatch: pytest.MonkeyPatch, fake_cursor: _FakeCursor
) -> None:
    """空リストでのsweepはインデックス全件削除の事故になりうるため、何も実行しないこと。"""
    _patch_connect(monkeypatch, fake_cursor)

    result = db.upsert_client_names_and_sweep([])

    assert fake_cursor.executed == []
    assert result == 0


def test_upsert_client_names_and_sweep_batches_upserts_and_deletes_stale_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_cursor = _FakeCursor(rowcount=3)
    conn = _patch_connect(monkeypatch, fake_cursor)
    monkeypatch.setattr(db, "_UPSERT_BATCH_SIZE", 2)

    records = [
        {"notion_page_id": f"page-{i}", "normalized_name": f"名前{i}", "raw_name": f"名前{i}"}
        for i in range(5)
    ]

    result = db.upsert_client_names_and_sweep(records)

    # 5件をバッチサイズ2で分割すると3バッチ(2,2,1) + 末尾のDELETEで計4クエリ。
    assert len(fake_cursor.executed) == 4
    upsert_calls = fake_cursor.executed[:3]
    delete_call = fake_cursor.executed[3]

    assert "VALUES (%s, %s, %s, %s, %s), (%s, %s, %s, %s, %s)" in upsert_calls[0][0]
    assert len(upsert_calls[0][1]) == 2 * 5  # 2行 x 5カラム
    assert len(upsert_calls[2][1]) == 1 * 5  # 端数バッチは1行分のみ

    delete_sql, delete_params = delete_call
    assert 'DELETE FROM "ClientNameIndex"' in delete_sql
    assert 'WHERE "syncedAt" < %s' in delete_sql
    assert len(delete_params) == 1

    assert conn.committed is True
    assert result == 3


# --- try_acquire_refresh_lock / release_refresh_lock ---------------------------------------


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


def test_refresh_lock_key_does_not_collide_with_project_mirror() -> None:
    from src.project_mirror import db as project_mirror_db

    assert db._REFRESH_LOCK_KEY != project_mirror_db._REFRESH_LOCK_KEY


# --- find_by_normalized_name ------------------------------------------------------------------


def test_find_by_normalized_name_returns_matching_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(
        fetch_rows=[
            {"notionPageId": "page-1", "rawName": "テスト商事株式会社"},
            {"notionPageId": "page-2", "rawName": "テスト商事(別法人)"},
        ]
    )
    _patch_connect(monkeypatch, cursor)

    result = db.find_by_normalized_name("テスト商事")

    sql, params = cursor.executed[0]
    assert 'WHERE "normalizedName" = %s' in sql
    assert params == ("テスト商事",)
    assert result == [
        {"notion_page_id": "page-1", "raw_name": "テスト商事株式会社"},
        {"notion_page_id": "page-2", "raw_name": "テスト商事(別法人)"},
    ]


def test_find_by_normalized_name_returns_empty_list_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor(fetch_rows=[])
    _patch_connect(monkeypatch, cursor)

    result = db.find_by_normalized_name("存在しない会社")

    assert result == []


# --- get_client_name_count -------------------------------------------------------------------


def test_get_client_name_count_returns_row_count(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetch_one_rows=[{"n": 9914}])
    _patch_connect(monkeypatch, cursor)

    result = db.get_client_name_count()

    assert result == 9914
