from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.kintone_webhook import handler, kintone_payload_to_sync_event

APP_ID_MAP = {"123": "project"}


def _payload() -> dict:
    return {
        "type": "record.updated",
        "app": {"id": "123"},
        "record": {
            "$id": {"type": "__ID__", "value": "45"},
            "$revision": {"type": "__REVISION__", "value": "3"},
            "レコード番号": {"type": "RECORD_NUMBER", "value": "45"},
            "作成日時": {"type": "CREATED_TIME", "value": "2026-08-01T00:00:00Z"},
            "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
            "作成者": {"type": "CREATOR", "value": {"code": "user1"}},
            "更新者": {"type": "MODIFIER", "value": {"code": "user1"}},
            "営業ステータス": {"type": "DROP_DOWN", "value": "商談中(B)"},
            "初期費用（イニシャル）": {"type": "NUMBER", "value": "500000"},
        },
    }


def test_kintone_payload_to_sync_event_builds_expected_event() -> None:
    event = kintone_payload_to_sync_event(_payload(), {}, app_id_to_db_key=APP_ID_MAP)

    assert event.source_tool is Tool.KINTONE
    assert event.db_key == "project"
    assert event.external_id == "45"
    assert event.occurred_at == datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc)
    assert event.properties == {
        "営業ステータス": "商談中(B)",
        "初期費用（イニシャル）": "500000",
    }
    assert event.sync_system_id is None


def test_kintone_payload_to_sync_event_excludes_system_fields() -> None:
    event = kintone_payload_to_sync_event(_payload(), {}, app_id_to_db_key=APP_ID_MAP)

    for system_field in ("$id", "$revision", "レコード番号", "作成日時", "更新日時", "作成者", "更新者"):
        assert system_field not in event.properties


def test_kintone_payload_to_sync_event_reads_sync_system_id_header() -> None:
    event = kintone_payload_to_sync_event(
        _payload(), {HEADER_NAME: "自社CRM-Engine"}, app_id_to_db_key=APP_ID_MAP
    )

    assert event.sync_system_id == "自社CRM-Engine"


def test_kintone_payload_to_sync_event_unknown_app_id_raises() -> None:
    with pytest.raises(ValueError):
        kintone_payload_to_sync_event(_payload(), {}, app_id_to_db_key={})


def test_kintone_payload_to_sync_event_uses_env_var_app_id_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KINTONE_APP_ID_PROJECT", "123")

    event = kintone_payload_to_sync_event(_payload(), {})

    assert event.db_key == "project"


def test_handler_dispatches_to_injected_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: APP_ID_MAP,
    )
    store = SQLiteIdMappingStore(":memory:")
    dispatcher = Dispatcher(store, {})
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None, dispatcher=dispatcher)

    store.close()
    assert response["statusCode"] == 200
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
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: APP_ID_MAP,
    )
    payload = _payload()
    del payload["record"]["$id"]
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 400


def test_handler_returns_400_for_unknown_app_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": json.dumps(_payload()), "headers": {}}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
            lambda: {},
        )
        response = handler(event, context=None)

    assert response["statusCode"] == 400


# --- BLOCKER7: 共有シークレット検証 -----------------------------------------------------


def test_handler_returns_401_when_secret_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KINTONE_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {WEBHOOK_SECRET_HEADER: "wrong-secret"}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_secret_header_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KINTONE_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_succeeds_when_secret_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KINTONE_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: APP_ID_MAP,
    )
    event = {
        "body": json.dumps(_payload()),
        "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 200
