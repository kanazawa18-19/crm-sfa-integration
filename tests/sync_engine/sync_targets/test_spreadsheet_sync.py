from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.db_schema.base import Tool
from src.sync_engine.conflict_resolver import RejectedData
from src.sync_engine.sync_targets.spreadsheet_sync import SYNC_LOG_SHEET_NAME, SpreadsheetSyncTarget


class FakeSpreadsheetClient:
    def __init__(self) -> None:
        self.rows: dict[str, dict[int, dict[str, Any]]] = {}
        self._next_row: dict[str, int] = {}

    def get_row(self, sheet: str, row: int) -> dict[str, Any] | None:
        return self.rows.get(sheet, {}).get(row)

    def append_row(self, sheet: str, values: dict[str, Any]) -> int:
        row = self._next_row.get(sheet, 0) + 1
        self._next_row[sheet] = row
        self.rows.setdefault(sheet, {})[row] = dict(values)
        return row

    def update_row(self, sheet: str, row: int, values: dict[str, Any]) -> None:
        self.rows.setdefault(sheet, {}).setdefault(row, {}).update(values)


def test_get_record_delegates_to_get_row() -> None:
    client = FakeSpreadsheetClient()
    client.rows["取引先マスター"] = {5: {"取引先名": "テスト商店"}}
    target = SpreadsheetSyncTarget(client, "取引先マスター")

    assert target.get_record("5") == {"取引先名": "テスト商店"}


def test_upsert_record_appends_when_external_id_none() -> None:
    client = FakeSpreadsheetClient()
    target = SpreadsheetSyncTarget(client, "取引先マスター")

    row = target.upsert_record(None, {"取引先名": "新規取引先"})

    assert row == "1"
    assert client.rows["取引先マスター"][1] == {"取引先名": "新規取引先"}


def test_upsert_record_updates_existing_row() -> None:
    client = FakeSpreadsheetClient()
    client.rows["取引先マスター"] = {5: {"取引先名": "旧名称"}}
    target = SpreadsheetSyncTarget(client, "取引先マスター")

    result = target.upsert_record("5", {"取引先名": "新名称"})

    assert result == "5"
    assert client.rows["取引先マスター"][5] == {"取引先名": "新名称"}


def test_delete_record_sets_delete_flag_instead_of_removing_row() -> None:
    client = FakeSpreadsheetClient()
    client.rows["取引先マスター"] = {5: {"取引先名": "テスト商店"}}
    target = SpreadsheetSyncTarget(client, "取引先マスター")

    target.delete_record("5")

    row = client.rows["取引先マスター"][5]
    assert row["削除フラグ"] is True
    assert row["取引先名"] == "テスト商店"  # 論理削除であり物理削除ではない


def test_append_conflict_log_writes_expected_columns() -> None:
    client = FakeSpreadsheetClient()
    target = SpreadsheetSyncTarget(client, "案件管理")
    rejected = RejectedData(
        record_id="MSA-PJ-001",
        property_name="営業ステータス",
        adopted_value="商談中(B)",
        rejected_value="失注",
        rejected_tool=Tool.KINTONE,
        occurred_at=datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc),
    )

    row = target.append_conflict_log(rejected)

    logged = client.rows[SYNC_LOG_SHEET_NAME][int(row)]
    assert logged == {
        "対象ID": "MSA-PJ-001",
        "項目名": "営業ステータス",
        "採用値": "商談中(B)",
        "却下値": "失注",
        "却下元ツール": "kintone",
        "発生日時": "2026-08-05T09:00:00+00:00",
    }
