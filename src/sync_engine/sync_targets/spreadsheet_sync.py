"""Googleスプレッドシート向け同期ターゲット（閲覧・分析UI／同期ログの退避先）。"""

from __future__ import annotations

from typing import Any, Protocol

from src.db_schema.base import Tool
from src.sync_engine.conflict_resolver import RejectedData
from src.sync_engine.sync_targets.base import SyncTarget

_DELETE_FLAG_COLUMN = "削除フラグ"

#: 行の同一性を保つためにシートへ書くNotionキーの列名（2026-08-31）。
#: **行番号を恒久IDにしない**ためのもの。人が行を挿入・削除・並べ替えても、
#: また「追記は成功したが行番号の保存前に落ちた」場合でも、ここから引き直せる。
SYNC_KEY_COLUMN = "同期キー"

# 05_同期・競合制御「データ退避」：却下データの退避先タブ名。
SYNC_LOG_SHEET_NAME = "同期ログ"


class SpreadsheetClient(Protocol):
    """Google Sheets APIの最小インターフェース。実HTTP通信（GAS/Sheets API）は実装側が担う。"""

    def get_row(self, sheet: str, row: int) -> dict[str, Any] | None: ...

    def append_row(self, sheet: str, values: dict[str, Any]) -> int:
        """行を追記し、採番された行番号を返す。"""
        ...

    def update_row(self, sheet: str, row: int, values: dict[str, Any]) -> None: ...

    def ensure_sync_key_column(self, sheet: str, header: str) -> int: ...

    def read_sync_key(self, sheet: str, row: int, header: str) -> str | None: ...

    def find_row_by_sync_key(self, sheet: str, header: str, key: str) -> int | None: ...

    def remember_sync_key_row(self, sheet: str, key: str, row: int) -> None: ...


class SpreadsheetSyncTarget(SyncTarget):
    """sheetはDBに対応するタブ名（例:「案件管理」）。DBごとにインスタンス化する。"""

    tool = Tool.SPREADSHEET

    def __init__(self, client: SpreadsheetClient, sheet: str) -> None:
        self._client = client
        self._sheet = sheet

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        return self._client.get_row(self._sheet, int(external_id))

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str:
        if external_id is None:
            row = self._client.append_row(self._sheet, properties)
            return str(row)
        self._client.update_row(self._sheet, int(external_id), properties)
        return external_id

    # --- 同期キーによる行の解決（行番号を恒久IDにしないため） -------------------------------

    def find_row_by_sync_key(self, sync_key: str) -> int | None:
        """シートに書かれた同期キーから行番号を引く。無ければNone。"""
        return self._client.find_row_by_sync_key(self._sheet, SYNC_KEY_COLUMN, sync_key)

    def row_matches_sync_key(self, row: int, sync_key: str) -> bool:
        """その行が本当にこのレコードの行か（1セルだけ読んで照合する）。

        人がシートに行を挿入・削除・並べ替えると行番号がずれる。照合せずに更新すると
        **別のレコードを上書きする**ため、更新前に必ず通す。
        まだ同期キーが入っていない行（この仕組みより前に作られた行）は、
        取り違えではないので一致扱いにし、書き込みのついでにキーを埋める。
        """
        actual = self._client.read_sync_key(self._sheet, row, SYNC_KEY_COLUMN)
        return actual is None or actual == sync_key

    def append_row_with_sync_key(self, properties: dict[str, Any], sync_key: str) -> int:
        """同期キーを必ず書き込んだうえで行を追記する。

        **キーを書かずに追記してはいけない。**書き忘れた行は、次回また
        「行が無い」と判断されて重複する。
        """
        self._client.ensure_sync_key_column(self._sheet, SYNC_KEY_COLUMN)
        row = self._client.append_row(self._sheet, {**properties, SYNC_KEY_COLUMN: sync_key})
        self._client.remember_sync_key_row(self._sheet, sync_key, row)
        return row

    def with_sync_key(self, properties: dict[str, Any], sync_key: str) -> dict[str, Any]:
        """更新時の書き込み値に同期キーを混ぜる（古い行へのキー埋めを兼ねる）。"""
        return {**properties, SYNC_KEY_COLUMN: sync_key}

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        self._client.update_row(self._sheet, int(external_id), {_DELETE_FLAG_COLUMN: True})

    def append_conflict_log(self, rejected: RejectedData) -> str:
        """05_同期・競合制御「データ退避」：却下データを「同期ログ」タブへ追記する。

        対象ID・項目名・採用値・却下値・発生日時（＋どのツールの値が採用され、どのツールの値が
        却下されたか）を記録する。
        """
        row = self._client.append_row(
            SYNC_LOG_SHEET_NAME,
            {
                "対象ID": rejected.record_id,
                "項目名": rejected.property_name,
                "採用値": rejected.adopted_value,
                "採用元ツール": rejected.adopted_tool.value,
                "却下値": rejected.rejected_value,
                "却下元ツール": rejected.rejected_tool.value,
                "発生日時": rejected.occurred_at.isoformat(),
            },
        )
        return str(row)
