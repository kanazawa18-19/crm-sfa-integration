"""Webhookの再送を弾く（2026-09-01）。

**Notionは配信に失敗すると最大8回・およそ24時間かけて再送する。**
購読を有効にしたことで新しく生まれたリスクで、再送のたびに同じ書き込みが走る。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.webhook_handlers.notion_webhook import handler_with_proxy

_BOT_ID = "3b4d8ea8-d4f3-81ee-b550-0027586fe38e"
_DATABASE_ID = next(s.notion_database_id for s in ALL_SCHEMAS if s.key == "action")


class _FakeClient:
    def __init__(self) -> None:
        self.fetches = 0

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        self.fetches += 1
        return {
            "id": page_id,
            "parent": {"type": "database_id", "database_id": _DATABASE_ID},
            "last_edited_time": "2026-09-01T09:00:00.000Z",
            "last_edited_by": {"id": "human-user-id"},
            "properties": {},
        }


def _event(event_id: str) -> dict[str, Any]:
    return {
        "headers": {},
        "body": json.dumps(
            {
                "id": event_id,
                "entity": {"id": "26d6f1e2-0000-0000-0000-000000000000", "type": "page"},
                "data": {"parent": {"id": _DATABASE_ID}},
            }
        ),
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)


@pytest.fixture
def claims(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    seen: set[str] = set()

    def _claim(event_id: str, source: str) -> bool:
        if event_id in seen:
            return False
        seen.add(event_id)
        return True

    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook.claim_event", _claim
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook.release_event",
        lambda event_id: seen.discard(event_id),
    )
    return seen


def test_the_same_event_is_processed_only_once(claims: set[str]) -> None:
    client = _FakeClient()

    first = handler_with_proxy(_event("evt_1"), None, notion_client=client, dispatcher=None)
    second = handler_with_proxy(_event("evt_1"), None, notion_client=client, dispatcher=None)

    assert first["statusCode"] == 200
    assert json.loads(second["body"]) == {"skipped": "duplicate_event"}
    # 2回目はページを取りに行かないこと（無駄なAPI呼び出しを増やさない）。
    assert client.fetches == 1


def test_different_events_are_both_processed(claims: set[str]) -> None:
    client = _FakeClient()

    handler_with_proxy(_event("evt_1"), None, notion_client=client, dispatcher=None)
    handler_with_proxy(_event("evt_2"), None, notion_client=client, dispatcher=None)

    assert client.fetches == 2


def test_a_failed_event_can_be_retried(claims: set[str]) -> None:
    """**処理に失敗したら記録を消す。** 消さないと再送まで弾いて変更が永久に失われる。"""

    class _Broken(_FakeClient):
        def get_raw_page(self, page_id: str) -> dict[str, Any]:
            self.fetches += 1
            raise RuntimeError("Notionが落ちている")

    broken = _Broken()
    result = handler_with_proxy(_event("evt_1"), None, notion_client=broken, dispatcher=None)
    assert result["statusCode"] == 500
    assert "evt_1" not in claims

    # 再送は処理できること。
    ok = handler_with_proxy(_event("evt_1"), None, notion_client=_FakeClient(), dispatcher=None)
    assert ok["statusCode"] == 200


def test_events_without_an_id_are_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """IDが無い形式のペイロードでも処理を止めない。"""
    from src.sync_engine import webhook_events

    assert webhook_events.claim_event("", "notion") is True
