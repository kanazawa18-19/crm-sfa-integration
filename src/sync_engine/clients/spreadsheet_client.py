"""Google Sheets API v4 (`https://sheets.googleapis.com/v4/spreadsheets/{id}/...`) へ
実HTTP通信を行う `SpreadsheetClient` Protocol実装。

`src/sync_engine/sync_targets/spreadsheet_sync.py` の `SpreadsheetClient` Protocolを満たす。
シート内の「行」は、1行目をプロパティ名の見出し行としたヘッダー行キーの列マッピングで表現する
（例: 1行目 `["取引先ID", "取引先名", ...]`、2行目以降が各レコード）。

認証について: 既定では`src/document_generation/google_auth.py`の
`get_google_access_token()`（サービスアカウント優先・自動リフレッシュ）でアクセストークンを
リクエストごとに解決する。テスト・ローカル動作確認向けに、明示的な`access_token`引数で
固定トークンを注入して上書きすることもできる。`access_token`未指定の場合、構築時に一度
`get_google_access_token()`を呼び有効な認証情報が解決できるか検証する（fail-fast。
戻り値そのものはリクエスト毎の解決を優先し保持しない）。
"""

from __future__ import annotations

import os
import re
from typing import Any

import requests

from src.document_generation.google_auth import get_google_access_token
from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    extract_error_message,
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
        if not self._spreadsheet_id:
            raise ValueError(
                "SPREADSHEET_ID environment variable (or spreadsheet_id argument) "
                "is required but not set"
            )
        # 明示的に固定トークンが渡された場合（テスト・ローカル動作確認）はそれを保持する。
        # Noneの場合は構築時には値を保持せず、`_headers()`でリクエストの都度
        # `get_google_access_token()`を呼び出す（本クライアントは常駐プロセスで
        # 使い回されるため、構築時に一度だけ解決すると約1時間で失効するトークンを
        # 使い続けてしまう。サービスアカウント利用時の自動リフレッシュを活かすため、
        # 毎回解決する）。ただし認証情報が丸ごと未設定（サービスアカウントJSONも
        # 手動トークンも無い）場合にそれを無視して構築を成功させてしまうと、
        # `production_wiring.build_spreadsheet_targets_by_db()`のfail-fast
        # （ValueErrorをcatchしてスプレッドシート同期を無効化する）が効かなくなり、
        # 実際のディスパッチ時までエラーが先送りされてDispatcher全体を巻き込んで
        # 落としかねない。そのため構築時に一度だけ`get_google_access_token()`を
        # 呼び、有効な認証情報が解決できることのみ確認する（戻り値は使い捨てる。
        # 毎回解決する方針自体は変えない）。
        if access_token is None:
            get_google_access_token()
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        # シート名 -> {同期キー: 行番号}。プロセス内シングルトンのwiringから使われるため
        # 長生きするが、`find_row_by_sync_key`はミス時に必ず読み直すので、
        # 古いキャッシュが「見つからない」を誤って返すことはない。
        self._sync_key_rows: dict[str, dict[str, int]] = {}

    def _headers(self) -> dict[str, str]:
        token = self._access_token if self._access_token is not None else get_google_access_token()
        return {"Authorization": f"Bearer {token}"}

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
        # shirokuma-secレビューBLOCKER対応（2026-08-28）: `append_row`と同じ理由
        # （モジュール内`append_row`のコメント参照）で、raise_for_error()通過後（2xx）でも
        # ボディが期待した形でない場合に生の例外が飛ばないよう正規化する。
        try:
            values = response.json().get("values") or []
            return values[0] if values else []
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            raise SpreadsheetApiError(response.status_code, extract_error_message(response)) from exc

    def list_sheet_names(self) -> tuple[str, list[str]]:
        """スプレッドシートのタイトルとシート名の一覧を返す（読み取りのみ）。

        同期の到達確認に使う。認証が通ることと、書き込み先のシートが実在することは別で、
        シート名が変わっただけでも同期は静かに失敗し続けるため、名前まで照合できるようにする。
        `_request()`はスプレッドシートID配下の相対パス専用なので、メタデータ取得
        （`/`直下）のために空パスを渡している。
        """
        response = self._request(
            "GET",
            "",
            params={"fields": "properties.title,sheets.properties.title"},
        )
        raise_for_error(response, SpreadsheetApiError)
        body = response.json()
        title = str(body.get("properties", {}).get("title", ""))
        names = [
            str(sheet.get("properties", {}).get("title", "")) for sheet in body.get("sheets", [])
        ]
        return title, names

    def count_rows(self, sheets: list[str]) -> dict[str, int]:
        """各シートのA列の行数（ヘッダ含む）をまとめて返す（読み取りのみ）。

        到達確認だけでは「認証は通るが1件も書かれていない」状態を見逃す。
        実際に2026-08-31、全6シートがヘッダ1行のままであることがこれで判明した。
        `rc=0`と「仕事が終わっている」は別、という原則をシートにも適用するための計測。
        """
        if not sheets:
            return {}
        # requestsは値がリストのキーを繰り返しクエリパラメータとして展開する
        # （ranges=...&ranges=...）。Sheets APIのbatchGetはこの形を要求する。
        params = {
            "majorDimension": "COLUMNS",
            "ranges": [f"'{sheet}'!A:A" for sheet in sheets],
        }
        response = self._request("GET", "/values:batchGet", params=params)
        raise_for_error(response, SpreadsheetApiError)
        value_ranges = response.json().get("valueRanges", [])
        counts: dict[str, int] = {}
        for sheet, value_range in zip(sheets, value_ranges):
            columns = value_range.get("values") or [[]]
            counts[sheet] = len(columns[0]) if columns else 0
        return counts

    # --- 同期キー（行の同一性を行番号に頼らないための列） ---------------------------------
    #
    # 行番号を恒久IDにすると、**人がシートに行を挿入・削除・並べ替えただけで別レコードを
    # 上書きする**。また「追記は成功したが行番号をDBに保存する前にプロセスが落ちた」場合、
    # 次回また追記されて重複する（Sheetsとpostgresの2者にまたがるので、try/exceptでは
    # 解決できない）。どちらも**シート側にNotionキーを書いておき、そこから引き直す**ことで
    # 直る（2026-08-31、Gemini・ChatGPTのレビュー指摘）。

    def ensure_sync_key_column(self, sheet: str, header: str) -> int:
        """同期キー列の列番号（1始まり）を返す。無ければヘッダ行の末尾に作る。

        シートを作り直さなくても、最初の書き込み時に自動で列が増える。
        """
        headers = self._get_header_row(sheet)
        for index, name in enumerate(headers):
            if name == header:
                return index + 1

        column = len(headers) + 1
        response = self._request(
            "PUT",
            f"/values/'{sheet}'!{column_letter(column)}1",
            params={"valueInputOption": "RAW"},
            json_body={"values": [[header]]},
        )
        raise_for_error(response, SpreadsheetApiError)
        # ヘッダが変わったので、この後の`append_row`/`update_row`が読み直せるよう
        # キャッシュは持たない（`_get_header_row`は元々毎回取りに行く）。
        self._sync_key_rows.pop(sheet, None)
        return column

    def read_sync_key(self, sheet: str, row: int, header: str) -> str | None:
        """指定行の同期キーを1セルだけ読む（更新前の照合用）。"""
        column = self.ensure_sync_key_column(sheet, header)
        response = self._request(
            "GET",
            f"/values/'{sheet}'!{column_letter(column)}{row}",
            params={"valueRenderOption": _VALUE_RENDER_OPTION},
        )
        raise_for_error(response, SpreadsheetApiError)
        values = response.json().get("values") or []
        if not values or not values[0]:
            return None
        value = str(values[0][0]).strip()
        return value or None

    def find_row_by_sync_key(self, sheet: str, header: str, key: str) -> int | None:
        """同期キーから行番号を引く。

        キャッシュが外れたときだけ列を読み直す。**「見つからない」で終わる前に必ず
        1度は実データを読む**ので、古いキャッシュのせいで重複行を作ることはない
        （高々1回の余分な読み取りで済む）。
        """
        cached = self._sync_key_rows.get(sheet)
        if cached is not None and key in cached:
            return cached[key]
        rows = self._load_sync_key_rows(sheet, header)
        return rows.get(key)

    def remember_sync_key_row(self, sheet: str, key: str, row: int) -> None:
        """追記した直後の対応をキャッシュへ入れる（同じイベント内の再検索を省くため）。"""
        self._sync_key_rows.setdefault(sheet, {})[key] = row

    def _load_sync_key_rows(self, sheet: str, header: str) -> dict[str, int]:
        column = self.ensure_sync_key_column(sheet, header)
        response = self._request(
            "GET",
            f"/values/'{sheet}'!{column_letter(column)}:{column_letter(column)}",
            params={"majorDimension": "COLUMNS", "valueRenderOption": _VALUE_RENDER_OPTION},
        )
        raise_for_error(response, SpreadsheetApiError)
        columns = response.json().get("values") or [[]]
        cells = columns[0] if columns else []
        rows: dict[str, int] = {}
        # 1行目はヘッダなので飛ばす。同じキーが複数行にあった場合は**最初の行**を採用する
        # （後から増えた重複行を正としてしまうと、正しい行の更新が止まるため）。
        for index, value in enumerate(cells[1:], start=2):
            key = str(value).strip()
            if key and key not in rows:
                rows[key] = index
        self._sync_key_rows[sheet] = rows
        return rows

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
        try:
            value_ranges = response.json().get("valueRanges") or []
            headers = _first_values_row(value_ranges, 0)
            row_values = _first_values_row(value_ranges, 1)
        except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            raise SpreadsheetApiError(response.status_code, extract_error_message(response)) from exc
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
        try:
            # shirokuma-secレビューWARN対応（2026-08-27）: raise_for_error()通過後（2xx）でも
            # ボディが期待した形でない場合に生のKeyErrorが飛ばないよう正規化する。詳細な理由は
            # `zoho_client.py`冒頭の同種コメント参照。
            updated_range = response.json()["updates"]["updatedRange"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SpreadsheetApiError(response.status_code, extract_error_message(response)) from exc
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
