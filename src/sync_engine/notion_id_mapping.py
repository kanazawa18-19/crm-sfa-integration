"""IDマッピングストアのNotion裏付け実装（`NotionIdMappingStore`）。

`src/sync_engine/production_wiring.py`のSQLite実装（`/tmp`配下、Vercelのコールドスタートで
消える）に代わる、GCP/AWS側の永続DBを契約するまでの暫定ブリッジとして導入する
（詳細な経緯・リスク・移行方針はdocs/id_mapping_persistence_note.md参照）。

リスク低減のため、以下2点を前提とする。
1. 実データの6DB（取引先マスタ等）とは別の、ID マッピング専用のNotion database
   （`_DEFAULT_ID_MAPPING_DATABASE_ID`）を使う。
2. コンテンツ同期の書き込みとは別の、専用のNotion APIトークン
   （`SYNC_ID_MAPPING_NOTION_API_KEY`）を使う。
これにより、本ストアの読み書きが実データ同期のNotion APIレート制限を消費することを避ける。

`src/sync_engine/id_mapping.py`（インターフェース＋`SQLiteIdMappingStore`）とは別ファイルに
分離している。id_mapping.pyはdispatcher.py等から広く参照される基盤モジュールであり、そこへ
`src/sync_engine/clients/_http.py`（requests依存のHTTP層）を持ち込むと、依存の向きが
逆転する（HTTP層を持たない軽量なインターフェース定義ファイルへ、実HTTP通信の依存を
混ぜ込むことになる）ため、`HttpNotionClient`（`clients/notion_client.py`）と同様に
実装だけを独立ファイルに切り出す。

■ 重複外部ID検知の既知の制約（並行Webhook受信時のレース窓）
`SQLiteIdMappingStore`はUNIQUE INDEXによるDBレベルの制約を持ち、事前チェック
（`_assert_no_duplicate_external_id`）をすり抜けた場合でも`sqlite3.IntegrityError`で
最終的に検知できる（belt-and-suspenders）。一方Notion側にはDBレベルの一意制約が無いため、
本実装の重複検知は`upsert()`内の事前チェック（クエリで既存レコードを検索する）のみであり、
これが唯一の防御線となる。したがって、ほぼ同時に2つのWebhookが同じ外部IDを持つ異なる
notion_keyへupsertした場合、両方の事前チェックが「重複なし」と判定してしまい、
`DuplicateExternalIdError`を検知できずに両方とも書き込まれてしまうレース窓が存在する
（分散ロック等による解消は本実装のスコープ外）。
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any

from src.db_schema.base import Tool
from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RATE_LIMIT_RETRIES,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)
from src.sync_engine.id_mapping import (
    _NOT_PROVIDED,
    ConflictError,
    DuplicateExternalIdError,
    IdMapping,
    IdMappingStore,
)
from src.sync_engine.webhook_handlers._common import parse_iso_datetime

logger = logging.getLogger(__name__)

_NOTION_VERSION = "2022-06-28"
_BASE_URL = "https://api.notion.com/v1"

# 既に用意済みの「データマッピング」専用Notion DB（本ファイルdocstring参照）。
# SYNC_ID_MAPPING_NOTION_DATABASE_ID環境変数で上書き可能。
_DEFAULT_ID_MAPPING_DATABASE_ID = "3b9d8ea8-d4f3-8059-8b04-ee5308d2cbf0"

# SQLiteIdMappingStoreの_EXTERNAL_ID_COLUMNSに対応する、Notion DB側のプロパティ名。
_EXTERNAL_ID_PROPERTIES: dict[Tool, str] = {
    Tool.KINTONE: "kintone_id",
    Tool.ZOHO: "zoho_id",
    Tool.SPREADSHEET: "spreadsheet_row",
}


class NotionIdMappingStoreApiError(ApiError):
    """Notion裏付けIDマッピングストアのAPI呼び出し失敗時に送出する例外。"""


def _title_text(prop: dict[str, Any] | None) -> str:
    if not prop:
        return ""
    parts = prop.get("title") or []
    return "".join(part.get("plain_text", "") for part in parts)


def _rich_text_value(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    parts = prop.get("rich_text") or []
    text = "".join(part.get("plain_text", "") for part in parts)
    return text or None


def _select_value(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    select = prop.get("select")
    return select.get("name") if select else None


def _number_value(prop: dict[str, Any] | None) -> int | None:
    if not prop:
        return None
    value = prop.get("number")
    return int(value) if value is not None else None


def _date_value(prop: dict[str, Any] | None) -> datetime | None:
    if not prop:
        return None
    date = prop.get("date")
    start = date.get("start") if date else None
    return parse_iso_datetime(start) if start else None


def _rich_text_content(value: Any) -> list[dict[str, Any]]:
    if value is None or value == "":
        return []
    return [{"type": "text", "text": {"content": str(value)}}]


def _page_to_mapping(page: dict[str, Any]) -> IdMapping:
    """Notion APIのページオブジェクト（`properties`を含む）を`IdMapping`へ変換する。"""
    properties = page.get("properties") or {}
    return IdMapping(
        notion_key=_title_text(properties.get("notion_key")),
        db_key=_select_value(properties.get("db_key")) or "",
        kintone_id=_rich_text_value(properties.get("kintone_id")),
        zoho_id=_rich_text_value(properties.get("zoho_id")),
        spreadsheet_row=_number_value(properties.get("spreadsheet_row")),
        last_synced_at=_date_value(properties.get("last_synced_at")),
    )


def _mapping_to_properties(mapping: IdMapping) -> dict[str, Any]:
    """`IdMapping`を、Notion API `pages`作成/更新用の`properties`ペイロードへ変換する。"""
    return {
        "notion_key": {"title": [{"type": "text", "text": {"content": mapping.notion_key}}]},
        "db_key": {"select": {"name": mapping.db_key}},
        "kintone_id": {"rich_text": _rich_text_content(mapping.kintone_id)},
        "zoho_id": {"rich_text": _rich_text_content(mapping.zoho_id)},
        "spreadsheet_row": {"number": mapping.spreadsheet_row},
        "last_synced_at": {
            "date": (
                {"start": mapping.last_synced_at.isoformat()} if mapping.last_synced_at else None
            )
        },
    }


def _title_equals_filter(value: str) -> dict[str, Any]:
    return {"property": "notion_key", "title": {"equals": value}}


def _text_equals_filter(property_name: str, value: str) -> dict[str, Any]:
    return {"property": property_name, "rich_text": {"equals": value}}


def _number_equals_filter(property_name: str, value: int) -> dict[str, Any]:
    return {"property": property_name, "number": {"equals": value}}


def _select_equals_filter(property_name: str, value: str) -> dict[str, Any]:
    return {"property": property_name, "select": {"equals": value}}


class NotionIdMappingStore(IdMappingStore):
    """`IdMappingStore`のNotion裏付け実装（暫定ブリッジ。本ファイルdocstring参照）。

    `database_id`省略時は`SYNC_ID_MAPPING_NOTION_DATABASE_ID`環境変数
    （さらに未設定なら`_DEFAULT_ID_MAPPING_DATABASE_ID`）を使う。`api_key`省略時は
    `SYNC_ID_MAPPING_NOTION_API_KEY`環境変数を使う（コンテンツ同期用の`NOTION_API_KEY`とは
    別の専用トークン。`api_key`引数と`SYNC_ID_MAPPING_NOTION_API_KEY`環境変数の両方とも
    未設定の場合は`ValueError`を送出する。`NOTION_API_KEY`へのフォールバックは行わない）。
    """

    def __init__(
        self,
        database_id: str | None = None,
        *,
        api_key: str | None = None,
        base_url: str = _BASE_URL,
        notion_version: str = _NOTION_VERSION,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        max_rate_limit_retries: int = DEFAULT_MAX_RATE_LIMIT_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    ) -> None:
        self._database_id = (
            database_id
            if database_id is not None
            else os.environ.get(
                "SYNC_ID_MAPPING_NOTION_DATABASE_ID", _DEFAULT_ID_MAPPING_DATABASE_ID
            )
        )
        self._api_key = (
            api_key if api_key is not None else os.environ.get("SYNC_ID_MAPPING_NOTION_API_KEY")
        )
        if not self._api_key:
            raise ValueError(
                "SYNC_ID_MAPPING_NOTION_API_KEY environment variable (or api_key argument) "
                "is required but not set"
            )
        content_sync_api_key = os.environ.get("NOTION_API_KEY")
        if content_sync_api_key and self._api_key == content_sync_api_key:
            # 専用トークンを使う目的（本ファイルdocstring・docs/id_mapping_persistence_note.md
            # 参照）は、実データ同期の書き込みとレート制限の枠を奪い合わないようにすること。
            # 同じ値が使われていると、この意図が達成できていない誤設定である可能性が高い
            # （起動をブロックはせず、気づけるよう警告のみ出す）。
            logger.warning(
                "SYNC_ID_MAPPING_NOTION_API_KEY is set to the same value as NOTION_API_KEY. "
                "A dedicated token is recommended to avoid rate-limit contention with "
                "content-sync writes (see docs/id_mapping_persistence_note.md)."
            )
        self._base_url = base_url.rstrip("/")
        self._notion_version = notion_version
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_rate_limit_retries = max_rate_limit_retries
        self._backoff_base = backoff_base

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
    ):
        return request_with_retry(
            method,
            f"{self._base_url}{path}",
            headers=self._headers(),
            json_body=json_body,
            timeout=self._timeout,
            max_retries=self._max_retries,
            max_rate_limit_retries=self._max_rate_limit_retries,
            backoff_base=self._backoff_base,
            idempotent=idempotent,
        )

    def _query_first(self, filter_obj: dict[str, Any]) -> dict[str, Any] | None:
        response = self._request(
            "POST",
            f"/databases/{self._database_id}/query",
            json_body={"filter": filter_obj, "page_size": 1},
        )
        raise_for_error(response, NotionIdMappingStoreApiError)
        results = response.json().get("results") or []
        return results[0] if results else None

    def _query_all(self, filter_obj: dict[str, Any], *, page_size: int = 100) -> list[dict[str, Any]]:
        """`HttpNotionClient.query_all_pages()`と同じ`has_more`/`next_cursor`方式でページングする。"""
        pages: list[dict[str, Any]] = []
        start_cursor: str | None = None
        while True:
            body: dict[str, Any] = {"filter": filter_obj, "page_size": page_size}
            if start_cursor is not None:
                body["start_cursor"] = start_cursor
            response = self._request(
                "POST", f"/databases/{self._database_id}/query", json_body=body
            )
            raise_for_error(response, NotionIdMappingStoreApiError)
            data = response.json()
            pages.extend(data.get("results") or [])
            if not data.get("has_more"):
                break
            start_cursor = data.get("next_cursor")
            if not start_cursor:
                # has_more=Trueかつnext_cursorが空という、Notion API本来の契約上は
                # 起きないはずのレスポンス。start_cursor=Noneのままループを続けると
                # 先頭ページの再取得を繰り返す無限ループになるため、打ち切る
                # （`HttpNotionClient.query_all_pages`と同じ対応）。
                logger.warning(
                    "_query_all: has_more=True but next_cursor is missing for "
                    "database_id=%r; stopping pagination to avoid an infinite loop",
                    self._database_id,
                )
                break
        return pages

    def get(self, notion_key: str) -> IdMapping | None:
        page = self._query_first(_title_equals_filter(notion_key))
        return _page_to_mapping(page) if page else None

    def upsert(
        self, mapping: IdMapping, *, expected_last_synced_at: datetime | None = _NOT_PROVIDED
    ) -> None:
        """notion_key をキーに新規作成または更新する。

        重複外部ID検知・CAS（`expected_last_synced_at`）の意味論は`SQLiteIdMappingStore.upsert`
        と同じだが、重複外部ID検知はNotion側にDBレベルの一意制約が無いため事前チェックのみで
        あり、並行Webhook受信時のレース窓が残る（本モジュールdocstring参照）。
        """
        self._assert_no_duplicate_external_id(mapping)
        existing_page = self._query_first(_title_equals_filter(mapping.notion_key))
        if expected_last_synced_at is not _NOT_PROVIDED:
            current = _page_to_mapping(existing_page) if existing_page else None
            current_synced_at = current.last_synced_at if current else None
            if current_synced_at != expected_last_synced_at:
                raise ConflictError(mapping.notion_key, expected_last_synced_at, current_synced_at)

        properties = _mapping_to_properties(mapping)
        if existing_page is None:
            body = {"parent": {"database_id": self._database_id}, "properties": properties}
            # 作成系（非冪等）操作のため、タイムアウト/5xx時の重複ページ作成を避けリトライしない
            # （HttpNotionClient.create_pageと同じ方針）。
            response = self._request("POST", "/pages", json_body=body, idempotent=False)
        else:
            response = self._request(
                "PATCH", f"/pages/{existing_page['id']}", json_body={"properties": properties}
            )
        raise_for_error(response, NotionIdMappingStoreApiError)

    def _assert_no_duplicate_external_id(self, mapping: IdMapping) -> None:
        """外部ID（kintone_id/zoho_id/spreadsheet_row）が既に別のnotion_keyに紐づいていないか検査する。

        `SQLiteIdMappingStore._assert_no_duplicate_external_id`と同じ事前チェックだが、
        Notion側にはDBレベルの一意制約によるフォールバック検知が無い（本モジュールdocstring
        の「既知の制約」参照）。
        """
        for tool, value in (
            (Tool.KINTONE, mapping.kintone_id),
            (Tool.ZOHO, mapping.zoho_id),
            (Tool.SPREADSHEET, mapping.spreadsheet_row),
        ):
            if value is None:
                continue
            existing = self.find_by_external_id(tool, str(value))
            if existing is not None and existing.notion_key != mapping.notion_key:
                raise DuplicateExternalIdError(tool, value, existing.notion_key)

    def delete(self, notion_key: str) -> None:
        """マッピングを削除する。Notion APIにハードデリートは無いため、ページをアーカイブする
        （`HttpNotionClient.archive_page`と同じ「削除」の表現方法）。存在しない場合は何もしない。
        """
        page = self._query_first(_title_equals_filter(notion_key))
        if page is None:
            return
        response = self._request("PATCH", f"/pages/{page['id']}", json_body={"archived": True})
        raise_for_error(response, NotionIdMappingStoreApiError)

    def find_by_external_id(self, tool: Tool, external_id: str) -> IdMapping | None:
        property_name = _EXTERNAL_ID_PROPERTIES.get(tool)
        if property_name is None:
            raise ValueError(f"unsupported tool for external id lookup: {tool}")
        if tool is Tool.SPREADSHEET:
            filter_obj = _number_equals_filter(property_name, int(external_id))
        else:
            filter_obj = _text_equals_filter(property_name, str(external_id))
        page = self._query_first(filter_obj)
        return _page_to_mapping(page) if page else None

    def update_last_synced_at(self, notion_key: str, synced_at: datetime) -> None:
        page = self._query_first(_title_equals_filter(notion_key))
        if page is None:
            raise KeyError(f"no id_mapping found for notion_key={notion_key!r}")
        body = {"properties": {"last_synced_at": {"date": {"start": synced_at.isoformat()}}}}
        response = self._request("PATCH", f"/pages/{page['id']}", json_body=body)
        raise_for_error(response, NotionIdMappingStoreApiError)

    def list_by_db(self, db_key: str) -> list[IdMapping]:
        pages = self._query_all(_select_equals_filter("db_key", db_key))
        return [_page_to_mapping(page) for page in pages]
