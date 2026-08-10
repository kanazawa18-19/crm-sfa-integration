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
        self, *, dispatcher: _SpyDispatcher | None = None, notion_page_client: Any = None
    ) -> None:
        self.dispatcher = dispatcher or _SpyDispatcher()
        self.notion_page_client = notion_page_client
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
        "module": "Deals",  # PROJECT_SCHEMA.zoho_api_module
        "operation": "update",
        "data": [
            {
                "id": "zoho-1",
                "Modified_Time": "2026-08-05T09:00:00+09:00",
                "営業ステータス": "商談中(B)",
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
