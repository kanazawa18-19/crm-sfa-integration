"""Google Sheets API v4 (`https://sheets.googleapis.com/v4/spreadsheets/{id}/...`) へ
実HTTP通信を行う `SpreadsheetClient` Protocol実装。

`src/sync_engine/sync_targets/spreadsheet_sync.py` の `SpreadsheetClient` Protocolを満たす。
シート内の「行」は、1行目をプロパティ名の見出し行としたヘッダー行キーの列マッピングで表現する
（例: 1行目 `["取引先ID", "取引先名", ...]`、2行目以降が各レコード）。

認証について: サービスアカウントのJWT署名による本来のトークン取得は複雑なため、本実装では
簡略化し、呼び出し元が有効なOAuth2アクセストークンを`GOOGLE_ACCESS_TOKEN`環境変数
（または明示的な`access_token`引数）で用意している前提のBearerトークン認証のみを実装する。
本番運用時は別途サービスアカウントJWTからのアクセストークン取得・リフレッシュ処理が必要。
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"
_UPDATED_RANGE_ROW_PATTERN = re.compile(r"![A-Za-z]+(\d+)")

# 読み取り時のデフォルト（FORMATTED_VALUE）だと数値が"500,000"のようなカンマ区切り文字列、
# 日付がロケール整形済み文字列で返ってしまい、build_notion_property_value(NUMBER/CURRENCY)
# へ渡すとNotion APIの数値型と不整合になり、また型正規化をしないconflict_resolverの
# _values_equalでは実際は変更がなくても毎回コンフリクトと誤検知され続ける。
# そのためUNFORMATTED_VALUEで生の値（数値はfloat/int、文字列はそのまま）を取得する。
_VALUE_RENDER_OPTION = "UNFORMATTED_VALUE"
# 日付/日時セルはUNFORMATTED_VALUE単体だとシリアル値（1899-12-30起点の経過日数）で返り、
# それをISO日付文字列へ変換するロジックが本実装には無いため、日付のみ従来通り
# FORMATTED_STRING（整形済み文字列）を維持する。数値のカンマ区切り解消（本BLOCKERの主眼）
# には影響しない。日付の正規化が必要になった場合は別途対応すること。
_DATE_TIME_RENDER_OPTION = "FORMATTED_STRING"


class SpreadsheetApiError(ApiError):
    """Google Sheets API呼び出し失敗時に送出する例外。"""


def column_letter(index: int) -> str:
    """1始まりの列番号をスプレッドシートの列記号（A, B, ..., Z, AA, ...）へ変換する。"""
    if index < 1:
        raise ValueError(f"column index must be >= 1, got {index}")
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


class HttpSpreadsheetClient:
    """Google Sheets API v4 (values.batchGet/append/batchUpdate) を用いた
    `SpreadsheetClient` Protocol実装。
    """

    def __init__(
        self,
        spreadsheet_id: str | None = None,
        *,
        access_token: str | None = None,
        base_url: str = _BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._spreadsheet_id = (
            spreadsheet_id if spreadsheet_id is not None else os.environ.get("SPREADSHEET_ID")
        )
        self._access_token = (
            access_token if access_token is not None else os.environ.get("GOOGLE_ACCESS_TOKEN")
        )
        if not self._spreadsheet_id:
            raise ValueError(
                "SPREADSHEET_ID environment variable (or spreadsheet_id argument) "
                "is required but not set"
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

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool = True,
    ) -> requests.Response:
        return request_with_retry(
            method,
            f"{self._base_url}/{self._spreadsheet_id}{path}",
            headers=self._headers(),
            json_body=json_body,
            params=params,
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
            idempotent=idempotent,
        )

    def _get_header_row(self, sheet: str) -> list[str]:
        response = self._request(
            "GET",
            f"/values/'{sheet}'!1:1",
            params={
                "valueRenderOption": _VALUE_RENDER_OPTION,
                "dateTimeRenderOption": _DATE_TIME_RENDER_OPTION,
            },
        )
        raise_for_error(response, SpreadsheetApiError)
        values = response.json().get("values") or []
        return values[0] if values else []

    def get_row(self, sheet: str, row: int) -> dict[str, Any] | None:
        response = self._request(
            "GET",
            "/values:batchGet",
            params={
                "ranges": [f"'{sheet}'!1:1", f"'{sheet}'!{row}:{row}"],
                "valueRenderOption": _VALUE_RENDER_OPTION,
                "dateTimeRenderOption": _DATE_TIME_RENDER_OPTION,
            },
        )
        raise_for_error(response, SpreadsheetApiError)
        value_ranges = response.json().get("valueRanges") or []
        headers = _first_values_row(value_ranges, 0)
        row_values = _first_values_row(value_ranges, 1)
        if not row_values:
            return None
        return {
            name: (row_values[i] if i < len(row_values) else None)
            for i, name in enumerate(headers)
            if name
        }

    def append_row(self, sheet: str, values: dict[str, Any]) -> int:
        headers = self._get_header_row(sheet)
        row_values = [values.get(name, "") for name in headers]
        # 作成系（非冪等）操作のため、タイムアウト/5xx時の重複行追加を避けリトライしない。
        response = self._request(
            "POST",
            f"/values/'{sheet}'!A1:append",
            params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
            json_body={"values": [row_values]},
            idempotent=False,
        )
        raise_for_error(response, SpreadsheetApiError)
        updated_range = response.json()["updates"]["updatedRange"]
        match = _UPDATED_RANGE_ROW_PATTERN.search(updated_range)
        if not match:
            raise SpreadsheetApiError(
                response.status_code, f"could not parse row number from updatedRange: {updated_range!r}"
            )
        return int(match.group(1))

    def update_row(self, sheet: str, row: int, values: dict[str, Any]) -> None:
        headers = self._get_header_row(sheet)
        data = [
            {"range": f"'{sheet}'!{column_letter(i + 1)}{row}", "values": [[values[name]]]}
            for i, name in enumerate(headers)
            if name in values
        ]
        if not data:
            return
        response = self._request(
            "POST",
            "/values:batchUpdate",
            json_body={"valueInputOption": "USER_ENTERED", "data": data},
        )
        raise_for_error(response, SpreadsheetApiError)


def _first_values_row(value_ranges: list[dict[str, Any]], index: int) -> list[Any]:
    if index >= len(value_ranges):
        return []
    values = value_ranges[index].get("values") or []
    return values[0] if values else []
