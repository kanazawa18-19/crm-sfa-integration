"""Notion API (`https://api.notion.com/v1/`) へ実HTTP通信を行う `NotionClient` Protocol実装。

`src/sync_engine/sync_targets/notion_sync.py` の `NotionClient` Protocolを満たす。
1インスタンス = 1 Notion database（`database_id`）に対応する
（`KintoneSyncTarget`/`ZohoSyncTarget`/`SpreadsheetSyncTarget` がDB単位でインスタンス化されるのと同様）。

内部の`properties: dict[str, Any]`（プロパティ名→生の値）とNotion APIが要求する
プロパティ型ごとの形式との相互変換は、`src/db_schema/registry.py`のスキーマ定義
（`PropertyType`）を参照して行う。Notion形式→内部値の変換は
`webhook_handlers/notion_webhook.py`の`parse_notion_property_value`を再利用し、
本モジュールはその逆方向（内部値→Notion形式）のみを追加で実装する。
"""

from __future__ import annotations

import os
from typing import Any

import requests

from src.db_schema.base import DatabaseSchema, PropertyType
from src.db_schema.registry import get_schema
from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"


class NotionApiError(ApiError):
    """Notion API呼び出し失敗時に送出する例外。"""


def _as_id_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _rich_text_content(value: Any) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    return [{"type": "text", "text": {"content": str(value)}}]


def build_notion_property_value(property_type: PropertyType, value: Any) -> dict[str, Any]:
    """内部値1件を、指定されたPropertyTypeに応じたNotion APIのプロパティ値形式へ変換する。

    `parse_notion_property_value`（Notion形式→内部値）の逆方向にあたる。
    """
    if property_type == PropertyType.TITLE:
        return {"title": _rich_text_content(value)}
    if property_type == PropertyType.TEXT:
        return {"rich_text": _rich_text_content(value)}
    if property_type == PropertyType.SELECT:
        return {"select": ({"name": value} if value else None)}
    if property_type == PropertyType.STATUS:
        return {"status": ({"name": value} if value else None)}
    if property_type in (PropertyType.NUMBER, PropertyType.CURRENCY):
        return {"number": value}
    if property_type in (PropertyType.DATE, PropertyType.DATETIME):
        return {"date": ({"start": value} if value else None)}
    if property_type == PropertyType.EMAIL:
        return {"email": value}
    if property_type == PropertyType.PHONE:
        return {"phone_number": value}
    if property_type == PropertyType.URL:
        return {"url": value}
    if property_type == PropertyType.CHECKBOX:
        return {"checkbox": bool(value)}
    if property_type == PropertyType.USER:
        return {"people": [{"id": user_id} for user_id in _as_id_list(value)]}
    if property_type == PropertyType.RELATION:
        return {"relation": [{"id": related_id} for related_id in _as_id_list(value)]}
    if property_type == PropertyType.JSON_TEXT:
        return {"rich_text": _rich_text_content(value)}
    raise ValueError(f"unsupported PropertyType for Notion conversion: {property_type!r}")


def build_notion_properties(properties: dict[str, Any], schema: DatabaseSchema) -> dict[str, Any]:
    """内部の`properties`辞書を、Notion APIのプロパティ形式の辞書へ一括変換する。"""
    return {
        name: build_notion_property_value(schema.get_property(name).property_type, value)
        for name, value in properties.items()
    }


class HttpNotionClient:
    """Notion API `GET/POST/PATCH /v1/pages` を用いた `NotionClient` Protocol実装。

    `db_key`（`src/db_schema/registry.py`のスキーマキー）と`database_id`（Notion側のDB ID）を
    それぞれ1つに固定してインスタンス化する。get_page()はNotionページの`properties`を
    内部形式（プロパティ名→生の値のフラットな辞書）へ変換して返す。
    """

    def __init__(
        self,
        db_key: str,
        database_id: str,
        *,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        notion_version: str = _NOTION_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._db_key = db_key
        self._database_id = database_id
        self._api_key = api_key if api_key is not None else os.environ.get("NOTION_API_KEY")
        if not self._api_key:
            raise ValueError(
                "NOTION_API_KEY environment variable (or api_key argument) is required but not set"
            )
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    @property
    def _schema(self) -> DatabaseSchema:
        return get_schema(self._db_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Notion-Version": self._notion_version,
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        idempotent: bool = True,
    ) -> requests.Response:
        return request_with_retry(
            method,
            f"{self._base_url}{path}",
            headers=self._headers(),
            json_body=json_body,
            timeout=self._timeout,
            max_retries=self._max_retries,
            backoff_base=self._backoff_base,
            idempotent=idempotent,
        )

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        response = self._request("GET", f"/pages/{page_id}")
        if response.status_code == 404:
            return None
        raise_for_error(response, NotionApiError)
        page = response.json()
        return {
            name: parse_notion_property_value(value)
            for name, value in (page.get("properties") or {}).items()
        }

    def create_page(self, properties: dict[str, Any]) -> str:
        body = {
            "parent": {"database_id": self._database_id},
            "properties": build_notion_properties(properties, self._schema),
        }
        # 作成系（非冪等）操作のため、タイムアウト/5xx時の重複ページ作成を避けリトライしない。
        response = self._request("POST", "/pages", json_body=body, idempotent=False)
        raise_for_error(response, NotionApiError)
        return response.json()["id"]

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        body = {"properties": build_notion_properties(properties, self._schema)}
        response = self._request("PATCH", f"/pages/{page_id}", json_body=body)
        raise_for_error(response, NotionApiError)

    def archive_page(self, page_id: str) -> None:
        response = self._request("PATCH", f"/pages/{page_id}", json_body={"archived": True})
        raise_for_error(response, NotionApiError)
