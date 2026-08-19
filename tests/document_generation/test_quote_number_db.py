"""src/document_generation/quote_number_db.py（見積書NOの当日発行連番）の検証。

実際のPostgresには接続しない。`psycopg.connect`をフェイクの接続/カーソルへ差し替えて、
発行されるSQL（`ON CONFLICT DO UPDATE ... RETURNING`によるロック挙動を含む本体）・
パラメータを検証する（`tests/project_mirror/test_db.py`と同じフェイクconnectパターン）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.document_generation import quote_number_db


class _FakeCursor:
    def __init__(self, fetch_one_rows: list[dict[str, Any] | None] | None = None) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self._fetch_one_rows = list(fetch_one_rows or [])

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

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

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.committed = True

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _set_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")


def _patch_connect(monkeypatch: pytest.MonkeyPatch, cursor: _FakeCursor) -> _FakeConnection:
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(quote_number_db.psycopg, "connect", lambda *args, **kwargs: conn)
    return conn


def test_next_sequence_for_date_upserts_and_returns_last_seq(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetch_one_rows=[{"lastSeq": 3}])
    conn = _patch_connect(monkeypatch, cursor)

    result = quote_number_db.next_sequence_for_date("20260819")

    assert result == 3
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert 'INSERT INTO "QuoteNumberSequence"' in sql
    assert "ON CONFLICT" in sql
    assert '"datePrefix"' in sql
    # 対象日付が既存の場合は"lastSeq"を+1する（重複防止の要）。
    assert '"lastSeq" = "QuoteNumberSequence"."lastSeq" + 1' in sql
    assert "RETURNING" in sql
    assert params == ("20260819",)
    assert conn.committed is True


def test_next_sequence_for_date_raises_when_no_row_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetch_one_rows=[None])
    _patch_connect(monkeypatch, cursor)

    with pytest.raises(RuntimeError, match="20260819"):
        quote_number_db.next_sequence_for_date("20260819")


def test_connect_raises_when_database_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="DATABASE_URL"):
        quote_number_db._connect()
