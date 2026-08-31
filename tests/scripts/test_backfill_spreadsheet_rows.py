"""scripts/backfill_spreadsheet_rows.py（スプレッドシート行の一括作成CLI）の検証。

実際のGoogle Sheets API・Notion API・Postgresへは一切アクセスしない。

このスクリプトは**非冪等な追記**をまとめて行う。途中で落ちたときに何が起きるかが
一番大事なので、そこを重点的に固定する（kuma-qaレビューBLOCKER対応、2026-08-31）。
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Any

import pytest

from scripts import backfill_spreadsheet_rows as script
from src.sync_engine.id_mapping import IdMapping


class _FakeSheetsClient:
    def __init__(self, fail_append: bool = False) -> None:
        self.appended: list[list[dict[str, Any]]] = []
        self.remembered: list[tuple[str, int]] = []
        self.primed: list[str] = []
        self.row_capacity: list[int] = []
        self.fail_append = fail_append
        self._next_row = 2

    def prime_sync_key_rows(self, sheet: str, header: str) -> int:
        self.primed.append(sheet)
        return 0

    def ensure_row_capacity(self, sheet: str, rows: int) -> None:
        self.row_capacity.append(rows)

    def append_rows(self, sheet: str, rows: list[dict[str, Any]]) -> list[int]:
        if self.fail_append:
            raise RuntimeError("Sheets API down")
        self.appended.append(list(rows))
        first = self._next_row
        self._next_row += len(rows)
        return list(range(first, first + len(rows)))

    def remember_sync_key_row(self, sheet: str, key: str, row: int) -> None:
        self.remembered.append((key, row))


class _FakeTarget:
    def __init__(self, client: _FakeSheetsClient) -> None:
        self._client = client
        self.rows_by_key: dict[str, int] = {}

    def find_row_by_sync_key(self, sync_key: str) -> int | None:
        return self.rows_by_key.get(sync_key)

    def with_sync_key(self, properties: dict[str, Any], sync_key: str) -> dict[str, Any]:
        return {**properties, "同期キー": sync_key}


class _FakeNotion:
    def __init__(self, pages: dict[str, dict[str, Any]]) -> None:
        self._pages = pages
        self.get_page_calls = 0

    def query_all_pages(self) -> list[dict[str, Any]]:
        return [
            {
                "id": page_id,
                "properties": {
                    "名前": {"type": "title", "title": [{"plain_text": props["名前"]}]}
                },
            }
            for page_id, props in self._pages.items()
        ]

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        self.get_page_calls += 1
        return self._pages.get(page_id)


class _FakeStore:
    def __init__(self, mappings: list[IdMapping]) -> None:
        self._mappings = mappings
        self.upserts: list[IdMapping] = []

    def list_by_db(self, db_key: str) -> list[IdMapping]:
        return list(self._mappings)

    def upsert(self, mapping: IdMapping, **_kwargs: Any) -> None:
        self.upserts.append(mapping)


def _mapping(key: str) -> IdMapping:
    return IdMapping(
        notion_key=key,
        db_key="product",
        last_synced_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch):
    def _wire(count: int = 3, fail_append: bool = False):
        client = _FakeSheetsClient(fail_append=fail_append)
        target = _FakeTarget(client)
        keys = [f"page-{i}" for i in range(count)]
        notion = _FakeNotion({k: {"名前": f"商品{i}"} for i, k in enumerate(keys)})
        store = _FakeStore([_mapping(k) for k in keys])
        monkeypatch.setattr(script, "build_spreadsheet_targets_by_db", lambda: {"product": target})
        monkeypatch.setattr(script, "build_notion_clients_by_db", lambda: {"product": notion})
        monkeypatch.setattr(script, "build_id_mapping_store", lambda: store)
        return client, target, notion, store

    return _wire


def test_dry_run_writes_nothing(wired) -> None:
    client, _target, _notion, store = wired(count=3)

    assert script.main(["--db-key", "product"]) == 0
    assert client.appended == []
    assert store.upserts == []


def test_apply_appends_in_one_batch_and_records_row_numbers(wired) -> None:
    client, _target, _notion, store = wired(count=3)

    assert script.main(["--db-key", "product", "--apply"]) == 0

    # 3件が1リクエストにまとまること（1件ずつだとSheetsのQuotaで1秒1行になる）。
    assert len(client.appended) == 1
    assert [row["同期キー"] for row in client.appended[0]] == ["page-0", "page-1", "page-2"]
    assert [m.spreadsheet_row for m in store.upserts] == [2, 3, 4]
    assert client.remembered == [("page-0", 2), ("page-1", 3), ("page-2", 4)]


def test_notion_pages_are_read_in_bulk_not_one_by_one(wired) -> None:
    """1件ずつget_page()を呼ぶとNotionのレート（約3req/秒）で頭打ちになる。"""
    _client, _target, notion, _store = wired(count=3)

    script.main(["--db-key", "product", "--apply"])

    assert notion.get_page_calls == 0


def test_existing_rows_are_not_appended_again(wired) -> None:
    """冪等。既にシートにキーがある行は作り直さず、行番号だけ入れ直す。"""
    client, target, _notion, store = wired(count=3)
    target.rows_by_key["page-1"] = 7

    script.main(["--db-key", "product", "--apply"])

    assert [row["同期キー"] for row in client.appended[0]] == ["page-0", "page-2"]
    assert any(m.notion_key == "page-1" and m.spreadsheet_row == 7 for m in store.upserts)


def test_append_failure_is_counted_and_does_not_stop_the_run(wired) -> None:
    """まとめ追記が落ちても、処理は最後まで進み「失敗あり」で終わる。

    行番号の登録は一切行わない（安全側）。各行に同期キーが入っているので、
    もう一度流せば同期キーで引き直され、重複せず続きから埋まる。
    """
    client, _target, _notion, store = wired(count=3, fail_append=True)

    # 失敗があった場合は非0で終わる（呼び出し元が気づけるように）。
    assert script.main(["--db-key", "product", "--apply"]) == 1
    assert store.upserts == []
    assert client.remembered == []


def test_row_capacity_is_reserved_before_writing(wired) -> None:
    """既定のシートは1000行しかない。流す件数ぶん先に広げる。"""
    client, _target, _notion, _store = wired(count=3)

    script.main(["--db-key", "product", "--apply"])

    assert client.row_capacity and client.row_capacity[0] >= 3


def test_limit_caps_the_number_of_records(wired) -> None:
    client, _target, _notion, _store = wired(count=5)

    script.main(["--db-key", "product", "--apply", "--limit", "2"])

    assert [row["同期キー"] for row in client.appended[0]] == ["page-0", "page-1"]
