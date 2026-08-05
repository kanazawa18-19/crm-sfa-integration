"""Googleスプレッドシート向け同期ターゲット（閲覧・分析UI／同期ログの退避先）。"""

from __future__ import annotations

from typing import Any, Protocol

from src.db_schema.base import Tool
from src.sync_engine.conflict_resolver import RejectedData
from src.sync_engine.sync_targets.base import SyncTarget

_DELETE_FLAG_COLUMN = "削除フラグ"

# 05_同期・競合制御「データ退避」：却下データの退避先タブ名。
SYNC_LOG_SHEET_NAME = "同期ログ"


class SpreadsheetClient(Protocol):
    """Google Sheets APIの最小インターフェース。実HTTP通信（GAS/Sheets API）は実装側が担う。"""

    def get_row(self, sheet: str, row: int) -> dict[str, Any] | None: ...

    def append_row(self, sheet: str, values: dict[str, Any]) -> int:
        """行を追記し、採番された行番号を返す。"""
        ...

    def update_row(self, sheet: str, row: int, values: dict[str, Any]) -> None: ...


class SpreadsheetSyncTarget(SyncTarget):
    """sheetはDBに対応するタブ名（例:「案件管理」）。DBごとにインスタンス化する。"""

    tool = Tool.SPREADSHEET

    def __init__(self, client: SpreadsheetClient, sheet: str) -> None:
        self._client = client
        self._sheet = sheet

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        return self._client.get_row(self._sheet, int(external_id))

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str:
        if external_id is None:
            row = self._client.append_row(self._sheet, properties)
            return str(row)
        self._client.update_row(self._sheet, int(external_id), properties)
        return external_id

    def delete_record(self, external_id: str) -> None:
        self._client.update_row(self._sheet, int(external_id), {_DELETE_FLAG_COLUMN: True})

    def append_conflict_log(self, rejected: RejectedData) -> str:
        """05_同期・競合制御「データ退避」：却下データを「同期ログ」タブへ追記する。

        対象ID・項目名・採用値・却下値・発生日時（＋どのツールの値が却下されたか）を記録する。
        """
        row = self._client.append_row(
            SYNC_LOG_SHEET_NAME,
            {
                "対象ID": rejected.record_id,
                "項目名": rejected.property_name,
                "採用値": rejected.adopted_value,
                "却下値": rejected.rejected_value,
                "却下元ツール": rejected.rejected_tool.value,
                "発生日時": rejected.occurred_at.isoformat(),
            },
        )
        return str(row)
