from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.spreadsheet_webhook import (
    handler,
    spreadsheet_payload_to_sync_event,
)

SHEET_MAP = {"案件管理": "project"}


def _payload() -> dict:
    return {
        "sheet": "案件管理",
        "row": 42,
        "editedAt": "2026-08-05T09:00:00+09:00",
        "values": {
            "案件ID": "MSA-PJ-001",
            "営業ステータス": "提案中",
            "初期費用（イニシャル）": 500000,
        },
    }


def test_spreadsheet_payload_to_sync_event_builds_expected_event() -> None:
    event = spreadsheet_payload_to_sync_event(_payload(), {}, sheet_to_db_key=SHEET_MAP)

    assert event.source_tool is Tool.SPREADSHEET
    assert event.db_key == "project"
    assert event.external_id == "42"
    assert event.occurred_at == datetime(
        2026, 8, 5, 9, 0, 0, tzinfo=timezone(timedelta(hours=9))
    )
    assert event.properties == {
        "案件ID": "MSA-PJ-001",
        "営業ステータス": "提案中",
        "初期費用（イニシャル）": 500000,
    }
    assert event.sync_system_id is None


def test_spreadsheet_payload_to_sync_event_reads_sync_system_id_header() -> None:
    event = spreadsheet_payload_to_sync_event(
        _payload(), {HEADER_NAME: "自社CRM-Engine"}, sheet_to_db_key=SHEET_MAP
    )

    assert event.sync_system_id == "自社CRM-Engine"


def test_spreadsheet_payload_to_sync_event_unknown_sheet_raises() -> None:
    with pytest.raises(ValueError):
        spreadsheet_payload_to_sync_event(_payload(), {}, sheet_to_db_key={})


def test_spreadsheet_payload_to_sync_event_uses_registry_display_name_by_default() -> None:
    event = spreadsheet_payload_to_sync_event(_payload(), {})

    assert event.db_key == "project"


def test_handler_dispatches_to_injected_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    store = SQLiteIdMappingStore(":memory:")
    dispatcher = Dispatcher(store, {})
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None, dispatcher=dispatcher)

    store.close()
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": True}  # unknown_record


# --- BLOCKER5: 不正・欠損ペイロード時のエラーハンドリング -------------------------------


def test_handler_returns_400_for_malformed_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": "{not valid json", "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_missing_required_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    payload = _payload()
    del payload["row"]
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_unknown_sheet_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": json.dumps(_payload()), "headers": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.sync_engine.webhook_handlers.spreadsheet_webhook._default_sheet_to_db_key",
            lambda: {},
        )
        response = handler(event, context=None)

    assert response["statusCode"] == 400


# --- BLOCKER7: 共有シークレット検証 -----------------------------------------------------


def test_handler_returns_401_when_secret_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADSHEET_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {WEBHOOK_SECRET_HEADER: "wrong-secret"}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_secret_header_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADSHEET_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_succeeds_when_secret_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPREADSHEET_WEBHOOK_SECRET", "correct-secret")
    event = {
        "body": json.dumps(_payload()),
        "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 200
