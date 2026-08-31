"""リレーションはシートへ書かない（2026-08-31）。"""

from __future__ import annotations

from typing import Any

from src.sync_engine.sync_targets.spreadsheet_sync import (
    SpreadsheetSyncTarget,
    drop_relation_properties,
)


class _FakeClient:
    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []
        self.updated: list[tuple[int, dict[str, Any]]] = []
        self.sync_key_rows: dict[str, int] = {}

    def get_row(self, sheet: str, row: int) -> dict[str, Any] | None:
        return None

    def append_row(self, sheet: str, values: dict[str, Any]) -> int:
        self.appended.append(dict(values))
        return len(self.appended) + 1

    def update_row(self, sheet: str, row: int, values: dict[str, Any]) -> None:
        self.updated.append((row, dict(values)))

    def ensure_sync_key_column(self, sheet: str, header: str) -> int:
        return 9

    def read_sync_key(self, sheet: str, row: int, header: str) -> str | None:
        return None

    def find_row_by_sync_key(self, sheet: str, header: str, key: str) -> int | None:
        return None

    def remember_sync_key_row(self, sheet: str, key: str, row: int) -> None:
        self.sync_key_rows[key] = row


_VALUES = {
    "名前": "フルスコ",
    "課金形態": "イニシャルスポット",
    # 実データでは1商品に25件ぶら下がっていて、セルが32桁のIDの羅列で埋まった。
    "案件管理": ["3b9d8ea8-d4f3-8116-be67-eb637fb5eca1", "3b9d8ea8-d4f3-8180-8648-ce2fc07dcdb5"],
}


def test_relation_properties_are_dropped() -> None:
    assert drop_relation_properties(_VALUES, "product") == {
        "名前": "フルスコ",
        "課金形態": "イニシャルスポット",
    }


def test_nothing_is_dropped_when_the_db_is_unknown() -> None:
    """db_keyが分からなければ、どれがリレーションか判断できない。何も落とさない。"""
    assert drop_relation_properties(_VALUES, None) == _VALUES


def test_append_does_not_write_relation_columns() -> None:
    client = _FakeClient()
    target = SpreadsheetSyncTarget(client, "サービス・商品", "product")

    target.append_row_with_sync_key(_VALUES, "sync-key-1")

    assert client.appended == [
        {"名前": "フルスコ", "課金形態": "イニシャルスポット", "同期キー": "sync-key-1"}
    ]


def test_update_does_not_write_relation_columns() -> None:
    client = _FakeClient()
    target = SpreadsheetSyncTarget(client, "サービス・商品", "product")

    target.upsert_record("3", _VALUES, db_key="product")

    assert client.updated == [(3, {"名前": "フルスコ", "課金形態": "イニシャルスポット"})]
