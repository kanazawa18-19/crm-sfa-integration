"""src/project_mirror/db.py（ProjectMirrorテーブルへの直接アクセス）の検証。

実際のPostgresには接続しない。`psycopg.connect`をフェイクの接続/カーソルへ差し替えて、
発行されるSQL・パラメータを検証する（`src/audit_log/db.py`と同じraw SQLパターンのため、
`_connect()`相当をモック化する方針は本テストが初出）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src import db_utils
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
    # db.psycopgをパッチすると、db.py内で呼ばれるsrc/db_utils.pyのpsycopg.connect()
    # （connect_for_advisory_lock()経由）にも間接的に効く。これはdb.pyが
    # `import psycopg`スタイルを維持している前提に依存しており、
    # `from psycopg import connect`のような書き方に変わると無言で効かなくなる
    # (INFO対応、2026-08-28。独立した検証はtests/test_db_utils.pyのdb_utils.psycopgへの
    # 直接パッチ側で担保している)。
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


def test_upsert_projects_and_sweep_synced_at_survives_postgres_ms_rounding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本番incident再現テスト(2026-08-25)。

    `syncedAt`カラムは`TIMESTAMP(3)`(ミリ秒精度)のため、UPSERTしたマイクロ秒精度の値は
    Postgres保存時に四捨五入(round-half-up、境界によっては繰り上がる)でミリ秒精度へ丸め
    られる。もし`upsert_projects_and_sweep()`が素の`datetime.now(timezone.utc)`を基準時刻に
    使うと、末尾のDELETEの比較には丸められていない元の値が使われ、丸め方向次第で
    「保存された丸め後の値 < DELETEの比較用の元の値」が真になり挿入直後の行まで誤って
    削除されてしまう(実データで確認済みのインシデント)。

    ここでは実DBに接続せず、`datetime.now()`を1000の倍数でない境界値
    (`927_999`マイクロ秒)に固定して決定的にテストする。UPSERT/DELETEで実際に使われた
    パラメータを取り出し、Postgresの丸め動作を切り捨てとしてシミュレートする(不動点
    かどうかの検証には切り捨て・繰り上げのどちらでも結果は変わらない、`test_db_utils.py`
    の`test_db_truncated_utcnow_is_idempotent_under_further_truncation`参照)。
    """
    fixed_now = datetime(2026, 8, 25, 12, 0, 0, 927_999, tzinfo=timezone.utc)

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            return fixed_now

    monkeypatch.setattr(db_utils, "datetime", _FixedDatetime)

    fake_cursor = _FakeCursor(rowcount=0)
    _patch_connect(monkeypatch, fake_cursor)

    records = [{"notion_page_id": "page-1", "data": {}, "last_edited_at": None}]
    db.upsert_projects_and_sweep(records)

    upsert_sql, upsert_params = fake_cursor.executed[0]
    delete_sql, delete_params = fake_cursor.executed[1]
    assert "INSERT INTO" in upsert_sql
    assert "DELETE FROM" in delete_sql

    upserted_synced_at = upsert_params[4]
    delete_threshold = delete_params[0]
    # 両方とも同じ基準時刻(db_truncated_utcnow()により927_999→927_000へ切り捨て済み)
    # から算出された値であること。
    assert upserted_synced_at.microsecond == 927_000
    assert upserted_synced_at == delete_threshold

    # PostgresのTIMESTAMP(3)保存時の丸めを切り捨てとしてシミュレートする
    # (927_000は1000の倍数の不動点のため、四捨五入で繰り上がっても結果は変わらない)。
    postgres_stored_value = upserted_synced_at.replace(
        microsecond=(upserted_synced_at.microsecond // 1000) * 1000
    )
    assert not (postgres_stored_value < delete_threshold)


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


def test_try_acquire_refresh_lock_prefers_database_url_unpooled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PgBouncerのtransaction poolingではadvisory lockの取得/解放が別セッションに分かれ
    無言で機能しなくなる(2026-08-28)。ロック用接続は`DATABASE_URL`(pooled)ではなく
    `DATABASE_URL_UNPOOLED`を優先して使うこと(`db_utils.connect_for_advisory_lock()`経由)。
    """
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://user:pass@direct-host/db")
    fake_cursor = _FakeCursor(fetch_one_rows=[{"locked": True}])
    conn = _FakeConnection(fake_cursor)
    captured_urls: list[str] = []

    def _fake_connect(url: str, **kwargs: Any) -> _FakeConnection:
        captured_urls.append(url)
        return conn

    monkeypatch.setattr(db.psycopg, "connect", _fake_connect)

    db.try_acquire_refresh_lock()

    assert captured_urls == ["postgresql://user:pass@direct-host/db"]


def test_try_acquire_refresh_lock_closes_connection_when_execute_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """呼び出し元はまだ`Connection`を受け取っていないため、`cur.execute()`が例外を投げた場合も
    ここで接続を必ずcloseすること(closeしないと接続がリークする、QAレビューWARN対応、
    2026-08-28。`src/document_generation/approval_db.py`の`try_acquire_approval_lock()`にも
    同じ形で存在した既存バグで、両方まとめて修正した)。"""

    class _RaisingCursor(_FakeCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            raise RuntimeError("boom")

    conn = _patch_connect(monkeypatch, _RaisingCursor())

    with pytest.raises(RuntimeError):
        db.try_acquire_refresh_lock()

    assert conn.closed is True


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
