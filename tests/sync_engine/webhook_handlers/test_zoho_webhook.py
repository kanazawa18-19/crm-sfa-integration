from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.zoho_webhook import handler, zoho_payload_to_sync_event

MODULE_MAP = {"案件": "project"}


def _payload() -> dict:
    return {
        "module": "案件",
        "operation": "update",
        "data": [
            {
                "id": "4876876000000488001",
                "Modified_Time": "2026-08-05T09:00:00+09:00",
                "営業ステータス": "商談中(B)",
                "初期費用（イニシャル）": 500000,
            }
        ],
    }


class SpyDispatcher:
    """dispatchが呼ばれたかどうかだけ記録するテスト用スタブ。"""

    def __init__(self) -> None:
        self.dispatched: list[object] = []

    def dispatch(self, event: object) -> DispatchResult:
        self.dispatched.append(event)
        return DispatchResult(skipped=False)


def test_zoho_payload_to_sync_event_builds_expected_event() -> None:
    event = zoho_payload_to_sync_event(_payload(), {}, module_to_db_key=MODULE_MAP)

    assert event.source_tool is Tool.ZOHO
    assert event.db_key == "project"
    assert event.external_id == "4876876000000488001"
    assert event.occurred_at == datetime(
        2026, 8, 5, 9, 0, 0, tzinfo=timezone(timedelta(hours=9))
    )
    assert event.properties == {"営業ステータス": "商談中(B)", "初期費用（イニシャル）": 500000}
    assert event.sync_system_id is None


def test_zoho_payload_to_sync_event_excludes_system_fields() -> None:
    event = zoho_payload_to_sync_event(_payload(), {}, module_to_db_key=MODULE_MAP)

    assert "id" not in event.properties
    assert "Modified_Time" not in event.properties


def test_zoho_payload_to_sync_event_reads_sync_system_id_header() -> None:
    event = zoho_payload_to_sync_event(
        _payload(), {HEADER_NAME: "自社CRM-Engine"}, module_to_db_key=MODULE_MAP
    )

    assert event.sync_system_id == "自社CRM-Engine"


def test_zoho_payload_to_sync_event_unknown_module_raises() -> None:
    with pytest.raises(ValueError):
        zoho_payload_to_sync_event(_payload(), {}, module_to_db_key={})


def test_zoho_payload_to_sync_event_uses_registry_zoho_api_module_by_default() -> None:
    """BLOCKER4: 逆引きはzoho_key（表示ラベル）ではなくzoho_api_module（実際のAPI module値）で行う。"""
    payload = _payload()
    payload["module"] = "Deals"  # PROJECT_SCHEMA.zoho_api_module

    event = zoho_payload_to_sync_event(payload, {})

    assert event.db_key == "project"


def test_zoho_payload_to_sync_event_display_label_is_not_a_valid_module_by_default() -> None:
    """BLOCKER4回帰確認: zoho_key（「案件」等の日本語ラベル）では逆引きできない。"""
    with pytest.raises(ValueError):
        zoho_payload_to_sync_event(_payload(), {})  # module="案件"はzoho_keyでありAPI名ではない


def test_handler_dispatches_to_injected_dispatcher_when_zoho_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    store = SQLiteIdMappingStore(":memory:")
    dispatcher = Dispatcher(store, {})
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None, dispatcher=dispatcher)

    store.close()
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": True}  # unknown_record


def test_handler_skips_entirely_when_zoho_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "False")
    spy = SpyDispatcher()
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None, dispatcher=spy)  # type: ignore[arg-type]

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"skipped": "zoho_disabled"}
    assert spy.dispatched == []


# --- BLOCKER5: 不正・欠損ペイロード時のエラーハンドリング -------------------------------


def test_handler_returns_400_for_malformed_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": "{not valid json", "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_missing_data_records(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    payload = _payload()
    payload["data"] = []
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_unknown_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": json.dumps(_payload()), "headers": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
            lambda: {},
        )
        response = handler(event, context=None)

    assert response["statusCode"] == 400


# --- BLOCKER7: 共有シークレット検証 -----------------------------------------------------


def test_handler_returns_401_when_secret_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {WEBHOOK_SECRET_HEADER: "wrong-secret"}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_secret_header_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_succeeds_when_secret_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    event = {
        "body": json.dumps(_payload()),
        "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 200
