"""ラベル駆動でスプレッドシートのセルへ値を差し込む。

テンプレートファイルごとに項目（"件名："等）の行番号・列位置が微妙に異なる
（メイリー系は初期費用セクションが21〜32行目、ホテマ系は21〜39行目等）ため、固定セル座標を
全テンプレート共通でハードコードせず、指定範囲を一括取得しラベル文字列に部分一致する行を
Python側で検索、その行の隣接セルへ値を書き込むという汎用ロジックで吸収する。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Protocol

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

logger = logging.getLogger(__name__)

_DEFAULT_RANGE = "A1:J60"
_SHEETS_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"


class LabelSheetsClient(Protocol):
    """`fill_labeled_cells`/`fill_cell_containing`が要求する最小限のSheets APIクライアント。"""

    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[Any]]: ...

    def update_value(self, spreadsheet_id: str, cell: str, value: str) -> None: ...


class SheetsApiError(ApiError):
    """Google Sheets API呼び出し失敗時に送出する例外。"""


class HttpSheetsValuesClient:
    """`LabelSheetsClient`のGoogle Sheets API v4実装。

    `src/sync_engine/clients/spreadsheet_client.py`のHttpSpreadsheetClientは単一の
    spreadsheet_id（環境変数SPREADSHEET_ID固定）・ヘッダー行ベースの実装であり、本機能が扱う
    「テンプレートごとに異なる動的なspreadsheet_id・ラベル検索による任意セル書き込み」には
    適さないため、専用の軽量クライアントを用意する。
    """

    def __init__(
        self,
        *,
        access_token: str | None = None,
        base_url: str = _SHEETS_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._access_token = (
            access_token if access_token is not None else os.environ.get("GOOGLE_ACCESS_TOKEN")
        )
        if not self._access_token:
            raise ValueError(
                "GOOGLE_ACCESS_TOKEN environment variable (or access_token argument) "
                "is required but not set"
            )
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[Any]]:
        response = request_with_retry(
            "GET",
            f"{self._base_url}/{spreadsheet_id}/values/{range_}",
            headers=self._headers(),
            params={"valueRenderOption": "UNFORMATTED_VALUE"},
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
        )
        raise_for_error(response, SheetsApiError)
        return response.json().get("values") or []

    def update_value(self, spreadsheet_id: str, cell: str, value: str) -> None:
        response = request_with_retry(
            "PUT",
            f"{self._base_url}/{spreadsheet_id}/values/{cell}",
            headers=self._headers(),
            params={"valueInputOption": "USER_ENTERED"},
            json_body={"values": [[value]]},
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
        )
        raise_for_error(response, SheetsApiError)

    def get_first_sheet_title(self, spreadsheet_id: str) -> str:
        """コピー後のスプレッドシートの先頭タブのシート名を返す。

        各テンプレートファイルには複数の既存案件タブが並んでいるため、先頭タブが
        差し込み対象の雛形タブである前提で解決する（実データでの事前確認が必要な暫定仕様）。
        """
        response = request_with_retry(
            "GET",
            f"{self._base_url}/{spreadsheet_id}",
            headers=self._headers(),
            params={"fields": "sheets.properties.title"},
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
        )
        raise_for_error(response, SheetsApiError)
        sheets = response.json().get("sheets") or []
        if not sheets:
            raise SheetsApiError(response.status_code, f"spreadsheet {spreadsheet_id!r} has no sheets")
        return sheets[0]["properties"]["title"]


def _column_letter(index: int) -> str:
    """0始まりの列インデックスを列記号（A, B, ..., Z, AA, ...）へ変換する。"""
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _find_target_column(row: list[Any], label_col_index: int) -> int:
    """ラベルセルより右側で最初に見つかった非空セルの列を返す。無ければラベルの右隣を返す。"""
    for i in range(label_col_index + 1, len(row)):
        if row[i] not in (None, ""):
            return i
    return label_col_index + 1


def fill_labeled_cells(
    sheets_client: LabelSheetsClient,
    spreadsheet_id: str,
    sheet_name: str,
    values_by_label: dict[str, str],
    *,
    range_: str = _DEFAULT_RANGE,
) -> None:
    """ラベル文字列に部分一致するセルを探し、同じ行の次の非空セル（無ければラベルの右セル）へ
    値を書き込む。

    ラベルが見つからない場合はエラーにせず警告ログのみ出す（テンプレートによって存在しない
    項目もあるため）。1つのラベルにつき最初に見つかった行のみを対象とする。
    """
    rows = sheets_client.get_values(spreadsheet_id, f"'{sheet_name}'!{range_}")
    remaining_labels = dict(values_by_label)
    for row_index, row in enumerate(rows):
        if not remaining_labels:
            break
        for col_index, cell in enumerate(row):
            if not isinstance(cell, str):
                continue
            matched_label = next((label for label in remaining_labels if label in cell), None)
            if matched_label is None:
                continue
            target_col = _find_target_column(row, col_index)
            target_cell = f"'{sheet_name}'!{_column_letter(target_col)}{row_index + 1}"
            sheets_client.update_value(spreadsheet_id, target_cell, remaining_labels.pop(matched_label))
            break

    for label in remaining_labels:
        logger.warning(
            "fill_labeled_cells: label %r not found in sheet %r (spreadsheet_id=%r); skipping",
            label,
            sheet_name,
            spreadsheet_id,
        )


def fill_cell_containing(
    sheets_client: LabelSheetsClient,
    spreadsheet_id: str,
    sheet_name: str,
    marker: str,
    new_value: str,
    *,
    range_: str = _DEFAULT_RANGE,
) -> bool:
    """`marker`を含むセルそのものを`new_value`で上書きする。

    `fill_labeled_cells`（ラベル→隣接セルへの書き込み）とは異なり、見積書の宛先欄
    （例:「〇〇　御中」のように会社名プレースホルダとサフィックスが同一セルに収まっている
    ケース）向けに、マーカーを含むセル自身を書き換える。見つからない場合はFalseを返す
    （呼び出し元でnotesへの追記等を行う）。
    """
    rows = sheets_client.get_values(spreadsheet_id, f"'{sheet_name}'!{range_}")
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(row):
            if isinstance(cell, str) and marker in cell:
                target_cell = f"'{sheet_name}'!{_column_letter(col_index)}{row_index + 1}"
                sheets_client.update_value(spreadsheet_id, target_cell, new_value)
                return True
    return False
