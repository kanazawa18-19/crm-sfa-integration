from __future__ import annotations

from typing import Any

from src.sync_engine.sync_targets.notion_sync import NotionSyncTarget


class FakeNotionClient:
    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        self.archived: list[str] = []
        self._next_id = 1

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        return self.pages.get(page_id)

    def create_page(self, properties: dict[str, Any]) -> str:
        page_id = f"page-{self._next_id}"
        self._next_id += 1
        self.pages[page_id] = dict(properties)
        return page_id

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        self.pages.setdefault(page_id, {}).update(properties)

    def archive_page(self, page_id: str) -> None:
        self.archived.append(page_id)


def test_get_record_delegates_to_get_page() -> None:
    client = FakeNotionClient()
    client.pages["CLI-001"] = {"取引先名": "テスト商店"}
    target = NotionSyncTarget(client)

    assert target.get_record("CLI-001") == {"取引先名": "テスト商店"}


def test_get_record_returns_none_when_missing() -> None:
    target = NotionSyncTarget(FakeNotionClient())

    assert target.get_record("no-such-page") is None


def test_upsert_record_creates_when_external_id_is_none() -> None:
    client = FakeNotionClient()
    target = NotionSyncTarget(client)

    page_id = target.upsert_record(None, {"取引先名": "新規取引先"})

    assert client.pages[page_id] == {"取引先名": "新規取引先"}


def test_upsert_record_updates_when_external_id_given() -> None:
    client = FakeNotionClient()
    client.pages["CLI-001"] = {"取引先名": "旧名称"}
    target = NotionSyncTarget(client)

    result = target.upsert_record("CLI-001", {"取引先名": "新名称"})

    assert result == "CLI-001"
    assert client.pages["CLI-001"] == {"取引先名": "新名称"}


def test_delete_record_calls_archive_page() -> None:
    client = FakeNotionClient()
    target = NotionSyncTarget(client)

    target.delete_record("CLI-001")

    assert client.archived == ["CLI-001"]
