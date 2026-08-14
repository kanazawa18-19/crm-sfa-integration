"""Notion向け同期ターゲット（01_システム構成：マスターDB／Single Source of Truth）。"""

from __future__ import annotations

from typing import Any, Protocol

from src.db_schema.base import Tool
from src.sync_engine.sync_targets.base import SyncTarget


class NotionClient(Protocol):
    """Notion API呼び出しの最小インターフェース。実HTTP通信は本Protocolの実装側が担う。"""

    def get_page(self, page_id: str) -> dict[str, Any] | None: ...

    def create_page(self, properties: dict[str, Any]) -> str:
        """新規ページを作成し、page_idを返す。"""
        ...

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None: ...

    def archive_page(self, page_id: str) -> None:
        """Notionの「アーカイブ」機能。論理削除に相当する。"""
        ...


class NotionSyncTarget(SyncTarget):
    """NotionClientへの薄いロジックラッパー。テストではモックNotionClientを注入する。"""

    tool = Tool.NOTION

    def __init__(self, client: NotionClient) -> None:
        self._client = client

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        return self._client.get_page(external_id)

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str:
        if external_id is None:
            return self._client.create_page(properties)
        self._client.update_page(external_id, properties)
        return external_id

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        self._client.archive_page(external_id)
