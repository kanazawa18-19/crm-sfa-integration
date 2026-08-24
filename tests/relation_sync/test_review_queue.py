"""src/relation_sync/review_queue.py（RelationReviewQueueテーブルへの直接アクセス）の検証。

実際のPostgresには接続しない。`psycopg.connect`をフェイクの接続/カーソルへ差し替える
（tests/project_mirror/test_db.pyと同じパターン）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.relation_sync import review_queue


class _FakeCursor:
    def __init__(
        self,
        fetch_rows: list[dict[str, Any]] | None = None,
        fetch_one_rows: list[dict[str, Any] | None] | None = None,
        rowcount: int = 1,
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
    monkeypatch.setattr(review_queue.psycopg, "connect", lambda *args, **kwargs: conn)
    return conn


# --- enqueue_for_review ------------------------------------------------------------------------


def test_enqueue_for_review_inserts_via_single_atomic_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SELECT→INSERTの2クエリではなく、`ON CONFLICT ... DO NOTHING`の1クエリで完結すること
    (shirokuma-sec/obasan-qualityレビューWARN対応、2026-08-25: 競合状態の解消)。"""
    cursor = _FakeCursor(rowcount=1)  # 新規挿入された(競合なし)
    conn = _patch_connect(monkeypatch, cursor)

    review_queue.enqueue_for_review(
        source_tool="kintone",
        source_record_id="77",
        target_db_key="client_master",
        raw_value="曖昧な会社名",
        candidate_notion_page_ids=["page-1", "page-2"],
        candidate_raw_names=["テスト商事株式会社", "テスト商事(別法人)"],
    )

    assert len(cursor.executed) == 1
    insert_sql, insert_params = cursor.executed[0]
    assert 'INSERT INTO "RelationReviewQueue"' in insert_sql
    assert "ON CONFLICT" in insert_sql
    assert "WHERE status = 'pending'" in insert_sql
    assert "DO NOTHING" in insert_sql
    assert insert_params[1] == "kintone"
    assert insert_params[2] == "77"
    assert insert_params[3] == "client_master"
    assert insert_params[4] == "曖昧な会社名"
    assert insert_params[5].obj == ["page-1", "page-2"]
    assert insert_params[6] == ["テスト商事株式会社", "テスト商事(別法人)"]
    assert conn.committed is True


def test_enqueue_for_review_treats_zero_rowcount_as_deduped_but_still_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ON CONFLICT ... DO NOTHING`によりDB側で重複がスキップされた場合(rowcount=0)、
    エラーにはせず正常終了すること。"""
    cursor = _FakeCursor(rowcount=0)  # 部分ユニークインデックスとの競合でスキップされた
    conn = _patch_connect(monkeypatch, cursor)

    review_queue.enqueue_for_review(
        source_tool="kintone",
        source_record_id="77",
        target_db_key="client_master",
        raw_value="曖昧な会社名",
        candidate_notion_page_ids=[],
        candidate_raw_names=[],
    )

    assert len(cursor.executed) == 1
    assert conn.committed is True


# --- list_pending_reviews ------------------------------------------------------------------


def test_list_pending_reviews_filters_by_pending_status(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = _FakeCursor(
        fetch_rows=[
            {
                "id": "row-1",
                "sourceTool": "kintone",
                "sourceRecordId": "77",
                "targetDbKey": "client_master",
                "rawValue": "曖昧な会社名",
                "candidateNotionPageIds": ["page-1"],
                "candidateRawNames": ["テスト商事株式会社"],
                "createdAt": "2026-08-25T00:00:00Z",
            }
        ]
    )
    _patch_connect(monkeypatch, cursor)

    result = review_queue.list_pending_reviews()

    sql, _ = cursor.executed[0]
    assert "WHERE status = 'pending'" in sql
    assert "candidateRawNames" in sql
    assert result == [
        {
            "id": "row-1",
            "sourceTool": "kintone",
            "sourceRecordId": "77",
            "targetDbKey": "client_master",
            "rawValue": "曖昧な会社名",
            "candidateNotionPageIds": ["page-1"],
            "candidateRawNames": ["テスト商事株式会社"],
            "createdAt": "2026-08-25T00:00:00Z",
        }
    ]
