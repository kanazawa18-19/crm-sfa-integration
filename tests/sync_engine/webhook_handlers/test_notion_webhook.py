from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.notion_webhook import (
    NotionPageClient,
    fetch_and_normalize_notion_page,
    handler,
    notion_payload_to_sync_event,
    parse_notion_property_value,
)

DB_ID_MAP = {"26d6f1e2-1111-1111-1111-111111111111": "project"}


def _payload() -> dict:
    return {
        "event_id": "evt_xxx",
        "type": "page.updated",
        "page_id": "26d6f1e2-0000-0000-0000-000000000000",
        "database_id": "26d6f1e2-1111-1111-1111-111111111111",
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "案件ID": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
            "営業ステータス": {"type": "status", "status": {"name": "提案中"}},
            "初期費用（イニシャル）": {"type": "number", "number": 500000},
        },
    }


# --- parse_notion_property_value --------------------------------------------------------


@pytest.mark.parametrize(
    ("prop", "expected"),
    [
        ({"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]}, "MSA-PJ-001"),
        ({"type": "rich_text", "rich_text": [{"plain_text": "備考テキスト"}]}, "備考テキスト"),
        ({"type": "title", "title": []}, None),
        ({"type": "select", "select": {"name": "ホテル・旅館"}}, "ホテル・旅館"),
        ({"type": "select", "select": None}, None),
        ({"type": "status", "status": {"name": "提案中"}}, "提案中"),
        ({"type": "number", "number": 500000}, 500000),
        ({"type": "checkbox", "checkbox": True}, True),
        ({"type": "date", "date": {"start": "2026-08-05"}}, "2026-08-05"),
        ({"type": "date", "date": None}, None),
        ({"type": "email", "email": "a@example.com"}, "a@example.com"),
        ({"type": "phone_number", "phone_number": "090-0000-0000"}, "090-0000-0000"),
        ({"type": "url", "url": "https://example.com"}, "https://example.com"),
        ({"type": "relation", "relation": [{"id": "rel-1"}, {"id": "rel-2"}]}, ["rel-1", "rel-2"]),
        ({"type": "people", "people": [{"id": "user-1"}]}, ["user-1"]),
    ],
)
def test_parse_notion_property_value(prop: dict, expected: object) -> None:
    assert parse_notion_property_value(prop) == expected


def test_parse_notion_property_value_unsupported_type_raises() -> None:
    with pytest.raises(ValueError):
        parse_notion_property_value({"type": "files", "files": []})


# --- notion_payload_to_sync_event -------------------------------------------------------


def test_notion_payload_to_sync_event_builds_expected_event() -> None:
    event = notion_payload_to_sync_event(_payload(), {}, db_id_to_db_key=DB_ID_MAP)

    assert event.source_tool is Tool.NOTION
    assert event.db_key == "project"
    assert event.external_id == "26d6f1e2-0000-0000-0000-000000000000"
    assert event.occurred_at == datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc)
    assert event.properties == {
        "案件ID": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "初期費用（イニシャル）": 500000,
    }
    assert event.sync_system_id is None


def test_notion_payload_to_sync_event_reads_sync_system_id_header_case_insensitively() -> None:
    event = notion_payload_to_sync_event(
        _payload(), {HEADER_NAME.lower(): "自社CRM-Engine"}, db_id_to_db_key=DB_ID_MAP
    )

    assert event.sync_system_id == "自社CRM-Engine"


def test_notion_payload_to_sync_event_unknown_database_id_raises() -> None:
    with pytest.raises(ValueError):
        notion_payload_to_sync_event(_payload(), {}, db_id_to_db_key={})


# --- handler -----------------------------------------------------------------------------


def test_handler_without_dispatcher_returns_200_and_no_skip_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": None}


def test_handler_dispatches_to_injected_dispatcher() -> None:
    import json

    store = SQLiteIdMappingStore(":memory:")
    dispatcher = Dispatcher(store, {})
    event = {"body": json.dumps(_payload()), "headers": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
        mp.setattr(
            "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
            lambda: DB_ID_MAP,
        )
        response = handler(event, context=None, dispatcher=dispatcher)

    store.close()
    assert response["statusCode"] == 200
    # IDマッピングが存在しないので unknown_record としてスキップされる
    assert json.loads(response["body"]) == {"skipped": True}


# --- BLOCKER5: 不正・欠損ペイロード時のエラーハンドリング -------------------------------


def test_handler_returns_400_for_malformed_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": "{not valid json", "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_missing_required_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    payload = _payload()
    del payload["page_id"]
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_unknown_database_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": json.dumps(_payload()), "headers": {}}
    # _default_db_id_to_db_key()をmonkeypatchしないため、実在しないdatabase_idとして扱われる
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
            lambda: {},
        )
        response = handler(event, context=None)

    assert response["statusCode"] == 400


# --- BLOCKER7: 共有シークレット検証 -----------------------------------------------------


def test_handler_returns_401_when_secret_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {WEBHOOK_SECRET_HEADER: "wrong-secret"}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_secret_header_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_succeeds_when_secret_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.notion_webhook._default_db_id_to_db_key",
        lambda: DB_ID_MAP,
    )
    event = {
        "body": json.dumps(_payload()),
        "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 200


# --- BLOCKER6: プロキシ層スタブ fetch_and_normalize_notion_page ------------------------


class _FakeNotionPageClient:
    def __init__(self, page: dict) -> None:
        self._page = page

    def get_page(self, page_id: str) -> dict:
        assert page_id == self._page["id"]
        return self._page


def test_fetch_and_normalize_notion_page_builds_expected_payload_shape() -> None:
    client: NotionPageClient = _FakeNotionPageClient(
        {
            "id": "26d6f1e2-0000-0000-0000-000000000000",
            "parent": {"type": "database_id", "database_id": "26d6f1e2-1111-1111-1111-111111111111"},
            "last_edited_time": "2026-08-05T09:00:00.000Z",
            "properties": {
                "案件ID": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
            },
        }
    )

    normalized = fetch_and_normalize_notion_page(
        "26d6f1e2-0000-0000-0000-000000000000", client
    )

    assert normalized["page_id"] == "26d6f1e2-0000-0000-0000-000000000000"
    assert normalized["database_id"] == "26d6f1e2-1111-1111-1111-111111111111"
    assert normalized["last_edited_time"] == "2026-08-05T09:00:00.000Z"
    assert normalized["properties"] == {
        "案件ID": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]}
    }
