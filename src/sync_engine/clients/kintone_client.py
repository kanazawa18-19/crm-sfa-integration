"""kintone REST API (`https://{KINTONE_DOMAIN}/k/v1/record.json`) へ実HTTP通信を行う
`KintoneClient` Protocol実装。

`src/sync_engine/sync_targets/kintone_sync.py` の `KintoneClient` Protocolを満たす。
認証はAPIトークン方式（`X-Cybozu-API-Token`ヘッダー）。トークンはアプリ（DB）単位で
発行されるため、1インスタンス = 1トークン（呼び出し元がDB単位でインスタンス化する設計）。

kintoneのレコード値は`{"フィールドコード": {"value": ...}}`という形式でラップされているため、
内部の`dict[str, Any]`（フィールドコード→生の値）との相互変換を本モジュールで行う。
"""

from __future__ import annotations

import os
from typing import Any

import requests

from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    extract_error_message,
    raise_for_error,
    request_with_retry,
)

# shirokuma-secレビューWARN対応（2026-08-27）: 詳細な理由は`zoho_client.py`冒頭の同種コメントを
# 参照。raise_for_error()は2xxなら何もしないため、HTTP 200でボディが期待した形でない
# （例: `record`/`id`キーを欠く異常応答）場合は生の`KeyError`が飛び、Dispatcher側で握っている
# 例外の型（`ApiError`/`requests.exceptions.RequestException`）をすり抜けて再びWebhookが
# 500になる。ここではraise_for_error()通過後の辞書アクセスを`KintoneApiError`へ正規化する。


class KintoneApiError(ApiError):
    """kintone API呼び出し失敗時に送出する例外。"""


def unwrap_kintone_record(record: dict[str, Any]) -> dict[str, Any]:
    """kintoneの`{"field": {"value": v}}`形式を内部のフラットな`{"field": v}`形式へ変換する。"""
    return {name: field.get("value") for name, field in record.items()}


def wrap_kintone_record(record: dict[str, Any]) -> dict[str, Any]:
    """内部のフラットな`{"field": v}`形式をkintoneの`{"field": {"value": v}}`形式へ変換する。

    Notion向け`build_notion_property_value`と異なり、本モジュールはPropertyTypeに応じた
    値の整形（例: USER_SELECT/関連レコード一覧型で単一値を自動的にリストへ正規化する等）は
    行わない。kintoneのフィールド種別ごとに`value`の期待形式が大きく異なる（例:
    ユーザー選択は`[{"code": "..."}]`、ルックアップは文字列など）ため、呼び出し元が
    対象フィールドの型に応じた正しい形式（単一値かリストか、要素の形状）で値を渡す責任を持つ。
    """
    return {name: {"value": value} for name, value in record.items()}


class HttpKintoneClient:
    """kintone REST API `record.json` (GET/POST/PUT) を用いた `KintoneClient` Protocol実装。"""

    def __init__(
        self,
        domain: str | None = None,
        *,
        api_token: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._domain = domain if domain is not None else os.environ.get("KINTONE_DOMAIN")
        self._api_token = (
            api_token if api_token is not None else os.environ.get("KINTONE_API_TOKEN")
        )
        if not self._domain:
            raise ValueError(
                "KINTONE_DOMAIN environment variable (or domain argument) is required but not set"
            )
        if not self._api_token:
            raise ValueError(
                "KINTONE_API_TOKEN environment variable (or api_token argument) is required but not set"
            )
        self._base_url = (base_url or f"https://{self._domain}/k/v1/record.json").rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def _headers(self, *, has_json_body: bool) -> dict[str, str]:
        """リクエストヘッダーを組み立てる。

        **`Content-Type`はJSONボディを送るとき（POST/PUT）だけ付ける。**
        kintoneはパラメータをクエリ文字列で渡すリクエスト（`get_record()`のGET）に
        `Content-Type: application/json`が付いていると、**ボディが無いのにJSONボディが
        あると宣言している不正なリクエスト**とみなして `HTTP 400 (code=CB_IL02)
        不正なリクエストです。` を返す（クエリ文字列方式ではMIMEタイプを指定しない、
        というのがkintone REST APIの共通仕様）。

        2026-08-28、この誤りにより`get_record()`が**常に**失敗していた。書き込み系
        (POST/PUT)はJSONボディがあり`Content-Type`が正しいため成功しており、読み取りだけが
        壊れていた。リレーション同期Round2（新規レコードのNotion自動作成）が本番の
        `get_record()`を初めて日常的に通る経路だったため、Round2の有効化ではじめて表面化した
        （external_id 62168/62169/62170/62171と連番で全件失敗していた）。
        """
        headers = {"X-Cybozu-API-Token": self._api_token or ""}
        if has_json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool = True,
    ) -> requests.Response:
        return request_with_retry(
            method,
            self._base_url,
            headers=self._headers(has_json_body=json_body is not None),
            json_body=json_body,
            params=params,
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
            idempotent=idempotent,
        )

    def get_record(self, app: str, record_id: str) -> dict[str, Any] | None:
        response = self._request("GET", params={"app": app, "id": record_id})
        if response.status_code == 404:
            return None
        raise_for_error(response, KintoneApiError)
        try:
            record = response.json()["record"]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise KintoneApiError(response.status_code, extract_error_message(response)) from exc
        return unwrap_kintone_record(record)

    def add_record(self, app: str, record: dict[str, Any]) -> str:
        body = {"app": app, "record": wrap_kintone_record(record)}
        # 作成系（非冪等）操作のため、タイムアウト/5xx時の重複レコード作成を避けリトライしない。
        response = self._request("POST", json_body=body, idempotent=False)
        raise_for_error(response, KintoneApiError)
        try:
            return str(response.json()["id"])
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise KintoneApiError(response.status_code, extract_error_message(response)) from exc

    def update_record(self, app: str, record_id: str, record: dict[str, Any]) -> None:
        body = {"app": app, "id": record_id, "record": wrap_kintone_record(record)}
        response = self._request("PUT", json_body=body)
        raise_for_error(response, KintoneApiError)
