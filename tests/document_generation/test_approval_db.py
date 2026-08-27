"""src/document_generation/approval_db.py（DocumentApprovalテーブルへの直接アクセス）の検証。

実際のPostgresには接続しない。`psycopg.connect`をフェイクの接続/カーソルへ差し替える
（`tests/document_generation/test_quote_number_db.py`と同じフェイクconnectパターン）。

複数承認者対応(2026-08-27)のデプロイ窓フォールバック（`approverEmails`がNULL/空で
旧`approverEmail`のみ埋まった行を読んだ場合、1要素配列として扱う挙動、
shirokuma-secレビューBLOCKER対応）を重点的に検証する。
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.document_generation import approval_db

_MIGRATION_SQL_PATH = (
    Path(__file__).resolve().parents[2]
    / "dashboard"
    / "prisma"
    / "migrations"
    / "20260827000000_document_approval_multi_approver"
    / "migration.sql"
)


class _FakeCursor:
    def __init__(
        self,
        *,
        fetch_one_rows: list[dict[str, Any] | None] | None = None,
        fetch_all_rows: list[list[dict[str, Any]]] | None = None,
    ) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self._fetch_one_rows = list(fetch_one_rows or [])
        self._fetch_all_rows = list(fetch_all_rows or [])

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append((sql, params))

    def fetchone(self) -> dict[str, Any] | None:
        return self._fetch_one_rows.pop(0) if self._fetch_one_rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self._fetch_all_rows.pop(0) if self._fetch_all_rows else []

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


@pytest.fixture(autouse=True)
def _set_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/testdb")


def _patch_connect(monkeypatch: pytest.MonkeyPatch, cursor: _FakeCursor) -> _FakeConnection:
    # approval_db.psycopgをパッチすると、approval_db内で呼ばれるsrc/db_utils.pyの
    # psycopg.connect()（connect_for_advisory_lock()経由）にも間接的に効く。これは
    # approval_db.pyが`import psycopg`スタイルを維持している前提に依存しており、
    # `from psycopg import connect`のような書き方に変わると無言で効かなくなる
    # (INFO対応、2026-08-28。独立した検証はtests/test_db_utils.pyのdb_utils.psycopgへの
    # 直接パッチ側で担保している)。
    conn = _FakeConnection(cursor)
    monkeypatch.setattr(approval_db.psycopg, "connect", lambda *args, **kwargs: conn)
    return conn


_NOW = datetime(2026, 8, 27, 0, 0, 0, tzinfo=timezone.utc)


def _row(*, approver_emails: list[str] | None, approver_email: str | None) -> dict[str, Any]:
    return {
        "id": "approval-1",
        "notionProjectId": "page-1",
        "category": "見積書",
        "driveFileId": "file-1",
        "driveApprovalId": "drive-approval-1",
        "approverEmails": approver_emails,
        "approverEmail": approver_email,
        "requestedByEmail": "rep@example.com",
        "status": "in_progress",
        "createdAt": _NOW,
        "resolvedAt": None,
    }


def test_row_to_approval_uses_approver_emails_when_present() -> None:
    row = _row(approver_emails=["a@example.com", "b@example.com"], approver_email="a@example.com")

    approval = approval_db._row_to_approval(row)

    assert approval.approver_emails == ["a@example.com", "b@example.com"]


def test_row_to_approval_falls_back_to_approver_email_when_approver_emails_is_none() -> None:
    """デプロイ窓（`prisma migrate deploy`適用〜新デプロイ公開までの数十秒）で旧コードが
    INSERTした行は`approverEmails`がNULL(psycopgではNone)のまま旧`approverEmail`のみ
    埋まっている。この場合1要素配列として読めることを確認する(shirokuma-secレビュー
    BLOCKER対応の中核テスト)。"""
    row = _row(approver_emails=None, approver_email="legacy@example.com")

    approval = approval_db._row_to_approval(row)

    assert approval.approver_emails == ["legacy@example.com"]


def test_row_to_approval_falls_back_to_approver_email_when_approver_emails_is_empty_list() -> None:
    row = _row(approver_emails=[], approver_email="legacy@example.com")

    approval = approval_db._row_to_approval(row)

    assert approval.approver_emails == ["legacy@example.com"]


def test_row_to_approval_returns_empty_list_when_both_columns_are_empty() -> None:
    """両カラムとも無い(理論上起こらないはずだが)場合でも例外にせず空配列を返す。"""
    row = _row(approver_emails=None, approver_email=None)

    approval = approval_db._row_to_approval(row)

    assert approval.approver_emails == []


def test_insert_document_approval_dual_writes_approver_email_and_returns_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor()
    conn = _patch_connect(monkeypatch, cursor)

    approval_id = approval_db.insert_document_approval(
        notion_project_id="page-1",
        category="見積書",
        drive_file_id="file-1",
        drive_approval_id="drive-approval-1",
        approver_emails=["a@example.com", "b@example.com"],
        requested_by_email="rep@example.com",
    )

    assert approval_id
    assert conn.committed is True
    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    # "approverEmail"(旧単一カラム)が列挙の中で二重にならないこと(_COLUMNSの流用による
    # バグの再発防止)。
    assert sql.count('"approverEmail"') == 1
    assert sql.count('"approverEmails"') == 1
    assert params == (
        approval_id,
        "page-1",
        "見積書",
        "file-1",
        "drive-approval-1",
        ["a@example.com", "b@example.com"],
        "a@example.com",
        "rep@example.com",
        approval_db.IN_PROGRESS,
    )


def test_find_in_progress_approval_returns_none_when_no_row(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetch_one_rows=[None])
    _patch_connect(monkeypatch, cursor)

    result = approval_db.find_in_progress_approval("page-1", "見積書")

    assert result is None


def test_find_in_progress_approval_applies_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(fetch_one_rows=[_row(approver_emails=None, approver_email="legacy@example.com")])
    _patch_connect(monkeypatch, cursor)

    result = approval_db.find_in_progress_approval("page-1", "見積書")

    assert result is not None
    assert result.approver_emails == ["legacy@example.com"]


def test_list_in_progress_approvals_maps_multiple_rows_with_mixed_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _row(approver_emails=["a@example.com"], approver_email="a@example.com"),
        _row(approver_emails=None, approver_email="legacy@example.com"),
    ]
    cursor = _FakeCursor(fetch_all_rows=[rows])
    _patch_connect(monkeypatch, cursor)

    result = approval_db.list_in_progress_approvals()

    assert [a.approver_emails for a in result] == [["a@example.com"], ["legacy@example.com"]]


def test_migration_sql_does_not_set_approver_emails_not_null() -> None:
    """`approverEmails`にSET NOT NULLを足し戻さないことを守る回帰テスト。

    このマイグレーションが`approverEmails`をNOT NULL化しないのは意図的（migration.sql内の
    コメント参照）。デプロイ窓（`prisma migrate deploy`がビルド時に走ってから新デプロイが
    公開されるまでの数十秒）は旧`insert_document_approval()`がまだ動いており、そのINSERT文
    は`approverEmails`列に一切触れない。ここでNOT NULL制約を付けると、その窓で送信される
    承認リクエストがnot-null違反で500になる（本テストが落ちたら、それは誰かがこの制約を
    足し戻そうとしている可能性が高い。本番で数十秒だけ500を出す事故を防ぐため、
    NOT NULL化は新コードが安定稼働してから別マイグレーションで行うこと。
    docs/quote_approval_note.md参照）。
    """
    sql = _MIGRATION_SQL_PATH.read_text(encoding="utf-8")

    not_null_statements = re.findall(
        r'ALTER\s+TABLE\s+"DocumentApproval"\s+ALTER\s+COLUMN\s+"approverEmails"\s+SET\s+NOT\s+NULL',
        sql,
        re.IGNORECASE,
    )

    assert not not_null_statements, (
        "migration.sqlにapproverEmailsへのSET NOT NULLが追加されています。デプロイ窓で旧"
        "insert_document_approval()のINSERTがnot-null違反で500になるため、意図的に外して"
        "います。NOT NULL化するなら新コードの安定稼働後に別マイグレーションで行ってください"
        "（docs/quote_approval_note.md参照）。"
    )


# --- try_acquire_approval_lock / release_approval_lock -----------------------------------
# TOCTOU対策(2026-08-28): request_quote_approval()の重複チェック→送信→INSERT区間の
# 多重実行防止用。`src/project_mirror/db.py`のtry_acquire_refresh_lock/release_refresh_lockと
# 同じ作法(fetchone()の{"locked": ...}・SQL文言・接続の開閉)であることを検証する。


def test_try_acquire_approval_lock_returns_connection_when_lock_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor(fetch_one_rows=[{"locked": True}])
    conn = _patch_connect(monkeypatch, cursor)

    result = approval_db.try_acquire_approval_lock("page-1", "見積書")

    assert result is conn
    assert conn.closed is False
    sql, params = cursor.executed[0]
    assert "pg_try_advisory_lock" in sql
    assert "hashtext" in sql
    assert params == (approval_db._APPROVAL_LOCK_NAMESPACE, "page-1:見積書")


def test_try_acquire_approval_lock_prefers_database_url_unpooled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PgBouncerのtransaction poolingではadvisory lockの取得/解放が別セッションに分かれ
    無言で機能しなくなる(2026-08-28)。ロック用接続は`DATABASE_URL`(pooled)ではなく
    `DATABASE_URL_UNPOOLED`を優先して使うこと(`db_utils.connect_for_advisory_lock()`経由)。
    """
    monkeypatch.setenv("DATABASE_URL_UNPOOLED", "postgresql://user:pass@direct-host/db")
    cursor = _FakeCursor(fetch_one_rows=[{"locked": True}])
    conn = _FakeConnection(cursor)
    captured_urls: list[str] = []

    def _fake_connect(url: str, **kwargs: Any) -> _FakeConnection:
        captured_urls.append(url)
        return conn

    monkeypatch.setattr(approval_db.psycopg, "connect", _fake_connect)

    approval_db.try_acquire_approval_lock("page-1", "見積書")

    assert captured_urls == ["postgresql://user:pass@direct-host/db"]


def test_try_acquire_approval_lock_closes_connection_when_execute_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """呼び出し元はまだ`Connection`を受け取っていないため、`cur.execute()`が例外を投げた場合も
    ここで接続を必ずcloseすること(closeしないと接続がリークする、QAレビューWARN対応、
    2026-08-28。`src/project_mirror/db.py`の`try_acquire_refresh_lock()`にも同じ形で存在した
    既存バグで、両方まとめて修正した)。"""

    class _RaisingCursor(_FakeCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            raise RuntimeError("boom")

    conn = _patch_connect(monkeypatch, _RaisingCursor())

    with pytest.raises(RuntimeError):
        approval_db.try_acquire_approval_lock("page-1", "見積書")

    assert conn.closed is True


def test_try_acquire_approval_lock_returns_none_and_closes_connection_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor(fetch_one_rows=[{"locked": False}])
    conn = _patch_connect(monkeypatch, cursor)

    result = approval_db.try_acquire_approval_lock("page-1", "見積書")

    assert result is None
    assert conn.closed is True


def test_release_approval_lock_unlocks_and_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = _FakeCursor()
    conn = _patch_connect(monkeypatch, cursor)

    approval_db.release_approval_lock(conn, "page-1", "見積書")

    sql, params = cursor.executed[0]
    assert "pg_advisory_unlock" in sql
    assert "hashtext" in sql
    assert params == (approval_db._APPROVAL_LOCK_NAMESPACE, "page-1:見積書")
    assert conn.closed is True


def test_release_approval_lock_closes_connection_even_if_unlock_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """例外が飛んでもロックの保持元である接続が必ず閉じられる(=解放される)ことを確認する。"""

    class _RaisingCursor(_FakeCursor):
        def execute(self, sql: str, params: Any = None) -> None:
            raise RuntimeError("boom")

    conn = _patch_connect(monkeypatch, _RaisingCursor())

    with pytest.raises(RuntimeError):
        approval_db.release_approval_lock(conn, "page-1", "見積書")

    assert conn.closed is True


def test_update_approval_status_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor()
    conn = _patch_connect(monkeypatch, cursor)

    approval_db.update_approval_status("approval-1", approval_db.APPROVED)

    assert conn.committed is True
    sql, params = cursor.executed[0]
    assert 'UPDATE "DocumentApproval"' in sql
    assert params == (approval_db.APPROVED, "approval-1")
