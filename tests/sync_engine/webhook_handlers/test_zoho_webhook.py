from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.sync_headers import HEADER_NAME
from src.sync_engine.webhook_handlers.zoho_webhook import handler, zoho_payload_to_sync_event

MODULE_MAP = {"案件": "project"}


def _payload(*, token: str | None = None) -> dict:
    payload: dict = {
        "module": "案件",
        "operation": "update",
        "data": [
            {
                "id": "4876876000000488001",
                "Modified_Time": "2026-08-05T09:00:00+09:00",
                "営業ステータス": "商談中(B)",
                "初期費用": 500000,
            }
        ],
    }
    if token is not None:
        payload["token"] = token
    return payload


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
    assert event.properties == {"営業ステータス": "商談中(B)", "初期費用": 500000}
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


def test_zoho_payload_to_sync_event_ignores_unknown_property_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = _payload()
    payload["data"][0]["未定義プロパティ"] = "何かの値"

    with caplog.at_level("WARNING"):
        event = zoho_payload_to_sync_event(payload, {}, module_to_db_key=MODULE_MAP)

    assert "未定義プロパティ" not in event.properties
    assert event.properties == {"営業ステータス": "商談中(B)", "初期費用": 500000}
    assert any("未定義プロパティ" in record.getMessage() for record in caplog.records)


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


@pytest.mark.parametrize("body", ["null", "[1, 2, 3]", "42", '"hello"', "true"])
def test_handler_returns_400_for_syntactically_valid_json_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """BLOCKER2回帰確認: 構文的に正しいJSONだが辞書でないbody（null/配列/数値/文字列/真偽値）は、
    verify_webhook_body_token()のpayload.get()呼び出しでAttributeErrorとして未捕捉のまま
    漏れることなく、JSONパース失敗と同様にfail-closedで400を返す。未認証でも到達可能な経路。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    event = {"body": body, "headers": {}}

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


# --- BLOCKER7: 共有トークン検証（body内のtokenフィールド方式） --------------------------
# Zoho Notifications（watch）APIは着信リクエストへ任意のHTTPヘッダーを付与できないため、
# 他ハンドラのX-Webhook-Secretヘッダー方式ではなく、通知ペイロードbody内の"token"フィールドを
# ZOHO_WEBHOOK_SECRETと照合するverify_webhook_body_token()を使う（zoho_webhook.py参照）。


def test_handler_returns_401_when_body_token_mismatches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload(token="wrong-secret")), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_body_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_body_token_has_different_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hmac.compare_digest利用の確認: 長さの異なるtokenも（単に短絡せず）確実に拒否される。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    event = {"body": json.dumps(_payload(token="short")), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_body_token_is_non_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """WARN6回帰確認: verify_webhook_body_token()はtokenフィールドが文字列以外（例: 数値）の
    場合もisinstanceガードによりfail-closed（401）で拒否する。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    payload = _payload()
    payload["token"] = 12345
    event = {"body": json.dumps(payload), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_returns_401_when_secret_env_unset_and_unsigned_not_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ZOHO_WEBHOOK_SECRET未設定時はfail-closed（ALLOW_UNSIGNED_WEBHOOKSが無ければ拒否）。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.delenv("ZOHO_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_UNSIGNED_WEBHOOKS", raising=False)
    event = {"body": json.dumps(_payload(token="anything")), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 401


def test_handler_succeeds_when_secret_env_unset_and_unsigned_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.delenv("ZOHO_WEBHOOK_SECRET", raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    event = {"body": json.dumps(_payload()), "headers": {}}

    response = handler(event, context=None)

    assert response["statusCode"] == 200


def test_handler_succeeds_when_body_token_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.zoho_webhook._default_module_to_db_key",
        lambda: MODULE_MAP,
    )
    event = {
        "body": json.dumps(_payload(token="correct-secret")),
        "headers": {},
    }

    response = handler(event, context=None)

    assert response["statusCode"] == 200
