"""src/api/app.pyに配線したWebhook受信エンドポイント・Cronバッチエンドポイントの検証。

実際のNotion/kintone/Zoho/スプレッドシートAPI・Slackへは一切アクセスしない
（`_wiring_dependency`をapp.dependency_overridesでフェイクに差し替え、
`run_report_batch`はmonkeypatchで置き換える）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api.app import _wiring_dependency, app
from src.db_schema.base import Tool
from src.db_schema.contact import CONTACT_SCHEMA
from src.db_schema.project import PROJECT_SCHEMA
from src.sync_engine.dispatcher import DispatchResult, PropertyDispatchResult
from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER


class _SpyDispatcher:
    """`SkipTrackingDispatcher`と同様に`last_result`を保持するテスト用フェイク
    （`src/api/app.py`の`_partial_skip_summary`がこの属性を参照する）。"""

    def __init__(self, result: DispatchResult | None = None) -> None:
        self.dispatched: list[Any] = []
        self._result = result or DispatchResult(skipped=True, reason="unknown_record")
        self.last_result: DispatchResult | None = None

    def dispatch(self, event: Any) -> DispatchResult:
        self.dispatched.append(event)
        self.last_result = self._result
        return self._result


class _FakeNotionPageClient:
    def __init__(self, page: dict[str, Any]) -> None:
        self._page = page

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        return self._page


class _FakeWiring:
    def __init__(
        self,
        *,
        dispatcher: _SpyDispatcher | None = None,
        notion_page_client: Any = None,
        calendar_sync_callable: Any = None,
        lead_sync_callable: Any = None,
    ) -> None:
        self.dispatcher = dispatcher or _SpyDispatcher()
        self.notion_page_client = notion_page_client
        self.calendar_sync_callable = calendar_sync_callable
        self.lead_sync_callable = lead_sync_callable
        self.id_mapping_store = None


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides() -> None:
    yield
    app.dependency_overrides.pop(_wiring_dependency, None)


def _override_wiring(fake: _FakeWiring) -> None:
    app.dependency_overrides[_wiring_dependency] = lambda: fake


# --- /api/webhooks/kintone / zoho / spreadsheet -------------------------------------------


def test_webhook_kintone_dispatches_via_injected_wiring(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    spy = _SpyDispatcher()
    _override_wiring(_FakeWiring(dispatcher=spy))
    payload = {
        "type": "record.updated",
        "app": {"id": "999"},
        "record": {
            "$id": {"type": "__ID__", "value": "45"},
            "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
            "営業ステータス": {"type": "DROP_DOWN", "value": "商談中(B)"},
        },
    }
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: {"999": "project"},
    )

    response = client.post("/api/webhooks/kintone", json=payload)

    assert response.status_code == 200
    assert response.json() == {"skipped": True}
    assert len(spy.dispatched) == 1
    assert spy.dispatched[0].db_key == "project"


def test_webhook_kintone_reflects_partial_sync_skip_in_response_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """obasan-quality/shirokuma-secレビュー対応: dispatch()の結果に部分的なスキップ
    （skipped_tools）が含まれる場合、Webhookレスポンスにも反映されることを確認する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    partial_skip_result = DispatchResult(
        skipped=False,
        properties=(
            PropertyDispatchResult(
                property_name="営業ステータス",
                resolution=None,
                written_tools=frozenset({Tool.ZOHO}),
                skipped_tools=frozenset({Tool.KINTONE}),
            ),
        ),
    )
    spy = _SpyDispatcher(result=partial_skip_result)
    _override_wiring(_FakeWiring(dispatcher=spy))
    payload = {
        "type": "record.updated",
        "app": {"id": "999"},
        "record": {
            "$id": {"type": "__ID__", "value": "45"},
            "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
            "営業ステータス": {"type": "DROP_DOWN", "value": "商談中(B)"},
        },
    }
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: {"999": "project"},
    )

    response = client.post("/api/webhooks/kintone", json=payload)

    assert response.status_code == 200
    body = response.json()
    assert body["partial_sync_skipped"] == [
        {
            "property": "営業ステータス",
            "written_tools": ["zoho"],
            "skipped_tools": ["kintone"],
        }
    ]


def test_webhook_kintone_does_not_add_partial_sync_skipped_field_when_fully_written(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    fully_written_result = DispatchResult(
        skipped=False,
        properties=(
            PropertyDispatchResult(
                property_name="営業ステータス",
                resolution=None,
                written_tools=frozenset({Tool.ZOHO, Tool.KINTONE}),
            ),
        ),
    )
    spy = _SpyDispatcher(result=fully_written_result)
    _override_wiring(_FakeWiring(dispatcher=spy))
    payload = {
        "type": "record.updated",
        "app": {"id": "999"},
        "record": {
            "$id": {"type": "__ID__", "value": "45"},
            "更新日時": {"type": "UPDATED_TIME", "value": "2026-08-05T09:00:00Z"},
            "営業ステータス": {"type": "DROP_DOWN", "value": "商談中(B)"},
        },
    }
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.kintone_webhook._default_app_id_to_db_key",
        lambda: {"999": "project"},
    )

    response = client.post("/api/webhooks/kintone", json=payload)

    assert response.status_code == 200
    assert "partial_sync_skipped" not in response.json()


def test_webhook_kintone_returns_401_when_secret_mismatches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KINTONE_WEBHOOK_SECRET", "correct-secret")
    _override_wiring(_FakeWiring())

    response = client.post(
        "/api/webhooks/kintone",
        json={"type": "record.updated", "app": {"id": "1"}, "record": {}},
        headers={WEBHOOK_SECRET_HEADER: "wrong-secret"},
    )

    assert response.status_code == 401


def test_webhook_zoho_dispatches_via_injected_wiring(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    spy = _SpyDispatcher()
    _override_wiring(_FakeWiring(dispatcher=spy))
    payload = {
        "server_time": 1754960400000,
        "module": "Deals",  # PROJECT_SCHEMA.zoho_api_module
        "operation": "update",
        "ids": ["zoho-1"],
        "affected_values": [
            {
                "record_id": "zoho-1",
                # field71は実際にconfig/zoho_field_mapping.jsonへ登録済みの実在するapi_name
                # （「営業ステータス」に対応）。
                "values": {"field71": "商談中(B)"},
            }
        ],
    }

    response = client.post("/api/webhooks/zoho", json=payload)

    assert response.status_code == 200
    assert len(spy.dispatched) == 1
    assert spy.dispatched[0].db_key == "project"


def test_webhook_spreadsheet_dispatches_via_injected_wiring(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    spy = _SpyDispatcher()
    _override_wiring(_FakeWiring(dispatcher=spy))
    payload = {
        "sheet": PROJECT_SCHEMA.spreadsheet_sheet_name,
        "row": 42,
        "editedAt": "2026-08-05T09:00:00+09:00",
        "values": {"営業ステータス": "提案中"},
    }

    response = client.post("/api/webhooks/spreadsheet", json=payload)

    assert response.status_code == 200
    assert len(spy.dispatched) == 1
    assert spy.dispatched[0].db_key == "project"


# --- /api/webhooks/notion -------------------------------------------------------------------


def test_webhook_notion_returns_500_when_notion_client_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    _override_wiring(_FakeWiring(notion_page_client=None))

    response = client.post(
        "/api/webhooks/notion",
        json={
            "id": "evt_xxx",
            "timestamp": "2026-08-05T09:00:00.000Z",
            "type": "page.properties_updated",
            "entity": {"id": "page-1", "type": "page"},
            "data": {},
        },
    )

    assert response.status_code == 500


def test_webhook_notion_dispatches_via_injected_wiring(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    spy = _SpyDispatcher()
    raw_page = {
        "id": "page-1",
        "parent": {"type": "database_id", "database_id": PROJECT_SCHEMA.notion_database_id},
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
        },
    }
    _override_wiring(
        _FakeWiring(dispatcher=spy, notion_page_client=_FakeNotionPageClient(raw_page))
    )

    response = client.post(
        "/api/webhooks/notion",
        json={
            "id": "evt_xxx",
            "timestamp": "2026-08-05T09:00:00.000Z",
            "type": "page.properties_updated",
            "entity": {"id": "page-1", "type": "page"},
            "data": {},
        },
    )

    assert response.status_code == 200
    assert len(spy.dispatched) == 1
    assert spy.dispatched[0].db_key == "project"
    assert spy.dispatched[0].external_id == "page-1"


def test_webhook_notion_invokes_calendar_sync_callable_for_project_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Part A回帰確認: 本番エンドポイント（webhook_notion）がwiring.calendar_sync_callable
    を実際にhandler_with_proxyへ渡し、db_key="project"のイベントで呼び出されることを確認する
    （このテスト追加以前は、calendar_sync/service.pyのフックが存在しても
    webhook_notionから一切配線されておらず、本番で発火することがなかった）。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    raw_page = {
        "id": "page-1",
        "parent": {"type": "database_id", "database_id": PROJECT_SCHEMA.notion_database_id},
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "案件名": {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]},
        },
    }
    calendar_sync_calls: list[tuple[dict, str]] = []

    def _calendar_sync(properties: dict, page_id: str) -> None:
        calendar_sync_calls.append((dict(properties), page_id))

    _override_wiring(
        _FakeWiring(
            notion_page_client=_FakeNotionPageClient(raw_page),
            calendar_sync_callable=_calendar_sync,
        )
    )

    response = client.post(
        "/api/webhooks/notion",
        json={
            "id": "evt_xxx",
            "timestamp": "2026-08-05T09:00:00.000Z",
            "type": "page.properties_updated",
            "entity": {"id": "page-1", "type": "page"},
            "data": {},
        },
    )

    assert response.status_code == 200
    assert len(calendar_sync_calls) == 1
    called_properties, called_page_id = calendar_sync_calls[0]
    assert called_page_id == "page-1"
    assert called_properties == {"案件名": "MSA-PJ-001"}


def test_webhook_notion_invokes_lead_sync_callable_for_contact_event(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Part B確認: 本番エンドポイント（webhook_notion）がwiring.lead_sync_callableを実際に
    handler_with_proxyへ渡し、db_key="contact"のイベントで呼び出されることを確認する。"""
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    raw_page = {
        "id": "page-2",
        "parent": {"type": "database_id", "database_id": CONTACT_SCHEMA.notion_database_id},
        "last_edited_time": "2026-08-05T09:00:00.000Z",
        "properties": {
            "名前": {"type": "title", "title": [{"plain_text": "山田太郎"}]},
            "メールアドレス": {"type": "email", "email": "yamada@example.com"},
        },
    }
    lead_sync_calls: list[tuple[dict, str]] = []

    def _lead_sync(properties: dict, page_id: str) -> None:
        lead_sync_calls.append((dict(properties), page_id))

    _override_wiring(
        _FakeWiring(
            notion_page_client=_FakeNotionPageClient(raw_page),
            lead_sync_callable=_lead_sync,
        )
    )

    response = client.post(
        "/api/webhooks/notion",
        json={
            "id": "evt_yyy",
            "timestamp": "2026-08-05T09:00:00.000Z",
            "type": "page.properties_updated",
            "entity": {"id": "page-2", "type": "page"},
            "data": {},
        },
    )

    assert response.status_code == 200
    assert len(lead_sync_calls) == 1
    called_properties, called_page_id = lead_sync_calls[0]
    assert called_page_id == "page-2"
    assert called_properties == {"名前": "山田太郎", "メールアドレス": "yamada@example.com"}


def test_webhook_notion_returns_401_when_secret_mismatches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "correct-secret")
    _override_wiring(_FakeWiring(notion_page_client=_FakeNotionPageClient({})))

    response = client.post(
        "/api/webhooks/notion",
        json={"id": "evt", "entity": {"id": "page-1"}},
        headers={WEBHOOK_SECRET_HEADER: "wrong-secret"},
    )

    assert response.status_code == 401


# --- /api/cron/daily-batch -----------------------------------------------------------------


def test_cron_daily_batch_returns_401_without_secret_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CRON_SECRET", raising=False)

    response = client.get("/api/cron/daily-batch")

    assert response.status_code == 401


def test_cron_daily_batch_returns_401_with_wrong_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRON_SECRET", "correct-secret")

    response = client.get(
        "/api/cron/daily-batch", headers={"Authorization": "Bearer wrong-secret"}
    )

    assert response.status_code == 401


def test_cron_daily_batch_runs_batch_when_secret_matches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.api.app.run_report_batch",
        lambda: {"date": "2026-08-11", "daily_report_sent": True, "weekly_report_sent": False},
    )

    response = client.get(
        "/api/cron/daily-batch", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "date": "2026-08-11",
        "daily_report_sent": True,
        "weekly_report_sent": False,
    }


# --- /api/cron/zoho-webhook-renewal ---------------------------------------------------------


def test_cron_zoho_webhook_renewal_returns_401_without_secret_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CRON_SECRET", raising=False)

    response = client.get("/api/cron/zoho-webhook-renewal")

    assert response.status_code == 401


def test_cron_zoho_webhook_renewal_returns_401_with_wrong_secret(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRON_SECRET", "correct-secret")

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer wrong-secret"}
    )

    assert response.status_code == 401


def test_cron_zoho_webhook_renewal_succeeds_when_secret_matches(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    monkeypatch.setattr(
        "src.api.app.build_zoho_client_from_env", lambda: object()
    )
    monkeypatch.setattr(
        "src.api.app.renew_zoho_watch_channel",
        lambda client, **kwargs: {"channel_id": "123", "channel_expiry": "2026-08-13T00:00:00+00:00"},
    )

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "success",
        "channel_id": "123",
        "channel_expiry": "2026-08-13T00:00:00+00:00",
    }


def test_cron_zoho_webhook_renewal_returns_500_when_channel_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """channel_idが未設定（ZOHO_WATCH_CHANNEL_IDも引数も無い）場合、成功したように見える
    no-opにせず明確なエラーとして表面化させることを確認する。"""
    from src.sync_engine.zoho_watch_channel import ZohoWatchChannelNotConfiguredError

    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    monkeypatch.setattr("src.api.app.build_zoho_client_from_env", lambda: object())

    def _raise(*args: object, **kwargs: object) -> None:
        raise ZohoWatchChannelNotConfiguredError("channel_id not configured")

    monkeypatch.setattr("src.api.app.renew_zoho_watch_channel", _raise)

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 500
    assert "channel_id not configured" in response.json()["detail"]


def test_cron_zoho_webhook_renewal_returns_502_on_zoho_api_failure(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zoho API呼び出し自体が失敗した場合も、スキップせず明確なエラーとして表面化させる。"""
    from src.sync_engine.clients.zoho_client import ZohoApiError

    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    monkeypatch.setattr("src.api.app.build_zoho_client_from_env", lambda: object())

    def _raise(*args: object, **kwargs: object) -> None:
        raise ZohoApiError(400, "invalid channel_id")

    monkeypatch.setattr("src.api.app.renew_zoho_watch_channel", _raise)

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 502
    assert "invalid channel_id" in response.json()["detail"]


def test_cron_zoho_webhook_renewal_502_body_never_contains_raw_secret_from_zoho_response(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER1: Zoho応答のwatchエントリがtokenをエコーバックしてきても、502のHTTPレスポンス
    ボディ（Vercel Cronのダッシュボード/ログから見える箇所）に生のシークレット値が
    含まれないこと。"""
    from src.sync_engine.clients.zoho_client import ZohoApiError
    from src.sync_engine.zoho_watch_channel import redact_watch_entry_token

    real_secret = "super-secret-webhook-token-value"
    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    monkeypatch.setattr("src.api.app.build_zoho_client_from_env", lambda: object())

    def _raise(*args: object, **kwargs: object) -> None:
        entry = {"status": "error", "code": "INVALID_DATA", "token": real_secret}
        raise ZohoApiError(200, str(redact_watch_entry_token(entry)))

    monkeypatch.setattr("src.api.app.renew_zoho_watch_channel", _raise)

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 502
    assert real_secret not in response.text


def test_cron_zoho_webhook_renewal_returns_clean_500_on_unexpected_exception(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER3: ZohoWatchChannelNotConfiguredError/ZohoApiError以外の想定外の例外も、
    未処理のまま生のトレースバック形状で漏れず、ログに記録した上で綺麗な500を返すこと。"""
    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    monkeypatch.setattr("src.api.app.build_zoho_client_from_env", lambda: object())

    def _raise(*args: object, **kwargs: object) -> None:
        raise AttributeError("'str' object has no attribute 'get'")

    monkeypatch.setattr("src.api.app.renew_zoho_watch_channel", _raise)

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "internal error during zoho webhook renewal"}


# --- BLOCKER3: 実際のZoho watch API呼び出し経路を通した、想定外レスポンス形の endpoint-level 検証
# （下のregister_or_renew_watch()自体は差し替えず、requests_mockで実際のHTTP応答を偽装する。
#   src.api.app.renew_zoho_watch_channelをmonkeypatchで丸ごと差し替える上のテスト群と異なり、
#   ここではrun_zoho_webhook_renewal → renew_zoho_watch_channel → register_or_renew_watch の
#   実コードパスを丸ごと通す）。


_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
_WATCH_URL = "https://www.zohoapis.jp/crm/v3/actions/watch"


@pytest.fixture
def _real_zoho_watch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CRON_SECRET", "correct-secret")
    monkeypatch.setenv("ZOHO_CLIENT_ID", "cid")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "rtoken")
    monkeypatch.setenv("ZOHO_WATCH_CHANNEL_ID", "123")
    monkeypatch.setenv("ZOHO_WEBHOOK_BASE_URL", "https://crm-sfa-integration.vercel.app")
    monkeypatch.delenv("ZOHO_ACCOUNTS_BASE_URL", raising=False)
    monkeypatch.delenv("ZOHO_API_BASE_URL", raising=False)


def _mock_zoho_token(requests_mock) -> None:
    requests_mock.post(_TOKEN_URL, json={"access_token": "access-token-1", "expires_in": 3600})


def test_cron_zoho_webhook_renewal_returns_clean_error_when_watch_entry_is_not_a_dict(
    client: TestClient, requests_mock, _real_zoho_watch_env: None
) -> None:
    _mock_zoho_token(requests_mock)
    requests_mock.put(_WATCH_URL, json={"watch": ["not-a-dict"]})

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 502
    assert "zoho api error" in response.json()["detail"]


def test_cron_zoho_webhook_renewal_returns_clean_error_when_response_body_is_not_json(
    client: TestClient, requests_mock, _real_zoho_watch_env: None
) -> None:
    _mock_zoho_token(requests_mock)
    requests_mock.put(_WATCH_URL, status_code=200, text="this is not json")

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 502
    assert "zoho api error" in response.json()["detail"]


def test_cron_zoho_webhook_renewal_returns_clean_error_when_response_body_is_a_bare_array(
    client: TestClient, requests_mock, _real_zoho_watch_env: None
) -> None:
    _mock_zoho_token(requests_mock)
    requests_mock.put(_WATCH_URL, json=["unexpected", "shape"])

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 502
    assert "zoho api error" in response.json()["detail"]


def test_cron_zoho_webhook_renewal_defaults_to_all_six_modules(
    client: TestClient, requests_mock, _real_zoho_watch_env: None
) -> None:
    """`run_zoho_webhook_renewal()`は`modules`を明示的に渡していないため、
    `renew_zoho_watch_channel()`の既定値（フィールドマッピングでカバー済みの6モジュール、
    `DEFAULT_MODULES`）で延長する。実コードパス（`register_or_renew_watch`まで）を通し、
    実際にZohoへ送るリクエストボディの`events`配列で確認する。"""
    from src.sync_engine.zoho_watch_channel import DEFAULT_MODULES

    _mock_zoho_token(requests_mock)
    requests_mock.put(
        _WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {"events": [{"channel_id": "123"}]},
                }
            ]
        },
    )

    response = client.get(
        "/api/cron/zoho-webhook-renewal", headers={"Authorization": "Bearer correct-secret"}
    )

    assert response.status_code == 200
    watch_calls = [req for req in requests_mock.request_history if req.url == _WATCH_URL]
    assert len(watch_calls) == 1
    sent_events = watch_calls[0].json()["watch"][0]["events"]
    assert sent_events == [f"{module}.all" for module in DEFAULT_MODULES]
