"""本番用Dispatcherファクトリ（src/sync_engine/production_wiring.py）の検証。

実際のNotion/kintone/Zoho/スプレッドシートAPIへは一切アクセスしない
（HttpXxxClientのコンストラクタが環境変数から認証情報を読むだけの経路のみを検証し、
実際のHTTPリクエストは発生させない）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.db_schema.base import Tool
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.clients._http import INTERACTIVE_MAX_RATE_LIMIT_RETRIES
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.notion_id_mapping import NotionIdMappingStore
from src.sync_engine.production_wiring import (
    ProductionSyncWiring,
    SkipTrackingDispatcher,
    _MultiDbKintoneSyncTarget,
    _MultiDbNotionSyncTarget,
    _warn_if_id_mapping_store_not_persistent,
    build_id_mapping_store,
    build_kintone_targets_by_db,
    build_notion_clients_by_db,
    build_production_dispatcher,
    build_spreadsheet_targets_by_db,
    build_zoho_targets_by_db,
    get_production_wiring,
    reset_production_wiring,
)
from src.sync_engine.sync_event import SyncEvent

NOW = datetime(2026, 8, 11, 9, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _reset_wiring_singleton() -> None:
    """モジュールレベルのシングルトンがテスト間で汚染されないようにする。"""
    reset_production_wiring()
    yield
    reset_production_wiring()


@pytest.fixture
def store() -> SQLiteIdMappingStore:
    s = SQLiteIdMappingStore(":memory:")
    yield s
    s.close()


# --- build_notion_clients_by_db --------------------------------------------------------------


def test_build_notion_clients_by_db_returns_empty_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)

    assert build_notion_clients_by_db() == {}


def test_build_notion_clients_by_db_returns_one_client_per_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTION_API_KEY", "secret-key")

    clients = build_notion_clients_by_db()

    assert set(clients.keys()) == {s.key for s in ALL_SCHEMAS if s.notion_database_id is not None}


# --- build_kintone_targets_by_db -------------------------------------------------------------


def test_build_kintone_targets_by_db_returns_empty_when_domain_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KINTONE_DOMAIN", raising=False)

    assert build_kintone_targets_by_db() == {}


def test_build_kintone_targets_by_db_skips_dbs_without_app_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KINTONE_DOMAIN", "example.cybozu.com")
    monkeypatch.setenv("KINTONE_APP_ID_PROJECT", "10")
    monkeypatch.setenv("KINTONE_API_TOKEN_PROJECT", "token-project")
    monkeypatch.delenv("KINTONE_APP_ID_CLIENT", raising=False)
    monkeypatch.delenv("KINTONE_API_TOKEN_CLIENT", raising=False)
    monkeypatch.delenv("KINTONE_APP_ID_ACTION", raising=False)
    monkeypatch.delenv("KINTONE_API_TOKEN_ACTION", raising=False)

    targets = build_kintone_targets_by_db()

    assert set(targets.keys()) == {"project"}


# --- build_zoho_targets_by_db ----------------------------------------------------------------


def test_build_zoho_targets_by_db_returns_empty_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "False")

    assert build_zoho_targets_by_db() == {}


def test_build_zoho_targets_by_db_returns_empty_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.delenv("ZOHO_CLIENT_ID", raising=False)
    monkeypatch.delenv("ZOHO_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ZOHO_REFRESH_TOKEN", raising=False)

    assert build_zoho_targets_by_db() == {}


def test_build_zoho_targets_by_db_builds_one_target_per_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_CLIENT_ID", "id")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "refresh")

    targets = build_zoho_targets_by_db()

    assert set(targets.keys()) == {s.key for s in ALL_SCHEMAS}


def test_build_zoho_targets_by_db_uses_default_com_base_urls_when_env_vars_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """データセンター指定用の環境変数が未設定の場合、既存の`.com`org向けの動作を
    サイレントに壊さないよう、HttpZohoClient自身のクラスデフォルト（`.com`）が使われること。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_CLIENT_ID", "id")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "refresh")
    monkeypatch.delenv("ZOHO_ACCOUNTS_BASE_URL", raising=False)
    monkeypatch.delenv("ZOHO_API_BASE_URL", raising=False)

    targets = build_zoho_targets_by_db()

    client = targets[ALL_SCHEMAS[0].key]._client  # noqa: SLF001 (テストのため内部状態を直接確認)
    assert client._accounts_base_url == "https://accounts.zoho.com"  # noqa: SLF001
    assert client._api_base_url == "https://www.zohoapis.com/crm/v2"  # noqa: SLF001


def test_build_zoho_targets_by_db_uses_configured_data_center_base_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ZOHO_ACCOUNTS_BASE_URL/ZOHO_API_BASE_URLを設定した場合、当該データセンター
    （例: .jp）向けのURLが構築されるHttpZohoClientへ実際に渡されること。"""
    monkeypatch.setenv("ENABLE_ZOHO", "True")
    monkeypatch.setenv("ZOHO_CLIENT_ID", "id")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "refresh")
    monkeypatch.setenv("ZOHO_ACCOUNTS_BASE_URL", "https://accounts.zoho.jp")
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")

    targets = build_zoho_targets_by_db()

    client = targets[ALL_SCHEMAS[0].key]._client  # noqa: SLF001 (テストのため内部状態を直接確認)
    assert client._accounts_base_url == "https://accounts.zoho.jp"  # noqa: SLF001
    assert client._api_base_url == "https://www.zohoapis.jp/crm/v2"  # noqa: SLF001


# --- build_spreadsheet_targets_by_db ---------------------------------------------------------


def test_build_spreadsheet_targets_by_db_returns_empty_when_credentials_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPREADSHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)

    assert build_spreadsheet_targets_by_db() == {}


def test_build_spreadsheet_targets_by_db_builds_one_target_per_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPREADSHEET_ID", "sheet-id")
    monkeypatch.setenv("GOOGLE_ACCESS_TOKEN", "token")

    targets = build_spreadsheet_targets_by_db()

    assert set(targets.keys()) == {s.key for s in ALL_SCHEMAS}


# --- build_production_dispatcher ---------------------------------------------------------------


def test_build_production_dispatcher_omits_tools_without_credentials(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("KINTONE_DOMAIN", raising=False)
    monkeypatch.setenv("ENABLE_ZOHO", "False")
    monkeypatch.delenv("SPREADSHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)

    dispatcher = build_production_dispatcher(id_mapping_store=store)

    assert dispatcher._targets == {}  # noqa: SLF001 (テストのため内部状態を直接確認)


def test_build_production_dispatcher_includes_configured_tools(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    monkeypatch.setenv("NOTION_API_KEY", "secret-key")
    monkeypatch.delenv("KINTONE_DOMAIN", raising=False)
    monkeypatch.setenv("ENABLE_ZOHO", "False")
    monkeypatch.delenv("SPREADSHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)

    dispatcher = build_production_dispatcher(id_mapping_store=store)

    assert set(dispatcher._targets.keys()) == {Tool.NOTION}  # noqa: SLF001


# --- _MultiDbNotionSyncTarget ------------------------------------------------------------------


class _FakeNotionClient:
    def __init__(self, db_key: str) -> None:
        self.db_key = db_key
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        return {"取引先名": f"from-{self.db_key}"}

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        self.update_calls.append((page_id, dict(properties)))

    def archive_page(self, page_id: str) -> None:
        pass


def test_multi_db_notion_sync_target_routes_write_by_id_mapping_db_key(
    store: SQLiteIdMappingStore,
) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", last_synced_at=NOW))
    client_master_client = _FakeNotionClient("client_master")
    project_client = _FakeNotionClient("project")
    target = _MultiDbNotionSyncTarget(
        {"client_master": client_master_client, "project": project_client}, store
    )

    target.upsert_record("CLI-001", {"取引先名": "新名称"})

    assert client_master_client.update_calls == [("CLI-001", {"取引先名": "新名称"})]
    assert project_client.update_calls == []


def test_multi_db_notion_sync_target_upsert_with_none_external_id_is_unsupported(
    store: SQLiteIdMappingStore,
) -> None:
    target = _MultiDbNotionSyncTarget({"client_master": _FakeNotionClient("client_master")}, store)

    result = target.upsert_record(None, {"取引先名": "新規"})

    assert result is None


def test_multi_db_notion_sync_target_skips_write_when_db_key_unresolvable(
    store: SQLiteIdMappingStore, caplog: pytest.LogCaptureFixture
) -> None:
    """obasan-quality/shirokuma-secレビュー対応: db_keyを解決できない場合、誤ったスキーマで
    強行書き込みするのではなく、書き込まずNone（未反映）を返すこと
    （Dispatcher._write_value()がこれをskipped_toolsへ計上するための契約）。"""
    fallback_client = _FakeNotionClient("client_master")
    target = _MultiDbNotionSyncTarget({"client_master": fallback_client}, store)

    with caplog.at_level("WARNING"):
        result = target.upsert_record("unknown-page-id", {"取引先名": "新名称"})

    assert result is None
    assert fallback_client.update_calls == []
    assert any("unknown-page-id" in r.getMessage() for r in caplog.records)


# --- _MultiDbKintoneSyncTarget ------------------------------------------------------------------


class _FakeKintoneSyncTarget:
    def __init__(self, db_key: str) -> None:
        self.db_key = db_key
        self.upsert_calls: list[tuple[str | None, dict[str, Any]]] = []

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        return {"db_key": self.db_key}

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str | None:
        self.upsert_calls.append((external_id, dict(properties)))
        return external_id

    def delete_record(self, external_id: str) -> None:
        pass


def test_multi_db_kintone_sync_target_routes_by_external_id_lookup(
    store: SQLiteIdMappingStore,
) -> None:
    store.upsert(
        IdMapping(notion_key="MSA-PJ-001", db_key="project", kintone_id="1001", last_synced_at=NOW)
    )
    project_target = _FakeKintoneSyncTarget("project")
    client_target = _FakeKintoneSyncTarget("client_master")
    router = _MultiDbKintoneSyncTarget(
        {"project": project_target, "client_master": client_target}, store
    )

    router.upsert_record("1001", {"営業ステータス": "商談中(B)"})

    assert project_target.upsert_calls == [("1001", {"営業ステータス": "商談中(B)"})]
    assert client_target.upsert_calls == []


def test_multi_db_kintone_sync_target_skips_write_when_db_key_unresolvable(
    store: SQLiteIdMappingStore, caplog: pytest.LogCaptureFixture
) -> None:
    router = _MultiDbKintoneSyncTarget({"project": _FakeKintoneSyncTarget("project")}, store)

    with caplog.at_level("WARNING"):
        result = router.upsert_record("no-such-id", {"営業ステータス": "商談中(B)"})

    assert result is None
    assert any("no-such-id" in r.getMessage() for r in caplog.records)


def test_multi_db_kintone_sync_target_get_record_returns_none_when_no_target(
    store: SQLiteIdMappingStore,
) -> None:
    router = _MultiDbKintoneSyncTarget({}, store)

    assert router.get_record("no-such-id") is None


# --- ProductionSyncWiring / get_production_wiring -----------------------------------------------


def test_get_production_wiring_returns_same_instance_across_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    monkeypatch.delenv("KINTONE_DOMAIN", raising=False)
    monkeypatch.setenv("ENABLE_ZOHO", "False")
    monkeypatch.delenv("SPREADSHEET_ID", raising=False)
    monkeypatch.setenv("SYNC_ID_MAPPING_DB_PATH", ":memory:")

    first = get_production_wiring()
    second = get_production_wiring()

    assert first is second


def _isolate_tool_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KINTONE_DOMAIN", raising=False)
    monkeypatch.setenv("ENABLE_ZOHO", "False")
    monkeypatch.delenv("SPREADSHEET_ID", raising=False)
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("SYNC_ID_MAPPING_DB_PATH", ":memory:")


def test_production_sync_wiring_notion_page_client_is_none_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    _isolate_tool_env(monkeypatch)

    wiring = ProductionSyncWiring()

    assert wiring.notion_page_client is None


def test_production_sync_wiring_notion_page_client_is_set_with_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTION_API_KEY", "secret-key")
    _isolate_tool_env(monkeypatch)

    wiring = ProductionSyncWiring()

    assert wiring.notion_page_client is not None


def test_production_sync_wiring_dispatcher_is_wrapped_with_skip_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    _isolate_tool_env(monkeypatch)

    wiring = ProductionSyncWiring()

    assert isinstance(wiring.dispatcher, SkipTrackingDispatcher)


# --- Dispatcher経由でのskipped_tools伝播（obasan-quality/shirokuma-secレビュー: 「同期
# スキップが成功として見える」問題の修正。ルーター単体ではなくDispatcher.dispatch()を
# 通した結果として正しく伝播することを検証する） -------------------------------------------


def test_multi_db_kintone_sync_target_reports_skip_through_real_dispatcher(
    store: SQLiteIdMappingStore,
) -> None:
    """実際の_MultiDbKintoneSyncTargetを本物のDispatcherへ組み込み、Notion発の変更で
    kintone向けの同期対象db_key用アプリが未構成（=このKintoneルーターに登録されていない）
    ケースが、Dispatcher.dispatch()の戻り値（written_tools/skipped_tools）へ正しく
    反映されることを検証する。"""
    mapping = IdMapping(
        notion_key="CLI-001",
        db_key="client_master",
        kintone_id="1001",
        zoho_id="zoho-1",
        spreadsheet_row=5,
        last_synced_at=NOW - timedelta(days=1),
    )
    store.upsert(mapping)

    class _FakeNotionTarget:
        tool = Tool.NOTION

        def get_record(self, external_id: str) -> dict[str, Any] | None:
            return None

        def upsert_record(self, external_id, properties):
            return external_id

        def delete_record(self, external_id: str) -> None:
            pass

    class _FakeZohoTarget(_FakeNotionTarget):
        tool = Tool.ZOHO

    class _FakeSpreadsheetTarget(_FakeNotionTarget):
        tool = Tool.SPREADSHEET

    # "project"用のkintoneアプリしか構成しない（"client_master"は未構成）ルーターを作る。
    kintone_router = _MultiDbKintoneSyncTarget({"project": object()}, store)  # type: ignore[arg-type]
    dispatcher = Dispatcher(
        store,
        {
            Tool.NOTION: _FakeNotionTarget(),
            Tool.KINTONE: kintone_router,
            Tool.ZOHO: _FakeZohoTarget(),
            Tool.SPREADSHEET: _FakeSpreadsheetTarget(),
        },
    )
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.written_tools == frozenset({Tool.ZOHO, Tool.SPREADSHEET})
    assert prop.skipped_tools == frozenset({Tool.KINTONE})
    assert result.has_partial_skips is True


# --- SkipTrackingDispatcher ---------------------------------------------------------------


def test_skip_tracking_dispatcher_delegates_and_stores_last_result(
    store: SQLiteIdMappingStore,
) -> None:
    store.upsert(
        IdMapping(
            notion_key="CLI-001",
            db_key="client_master",
            kintone_id="1001",
            zoho_id="zoho-1",
            spreadsheet_row=5,
            last_synced_at=NOW - timedelta(days=1),
        )
    )
    inner = Dispatcher(store, {})
    wrapped = SkipTrackingDispatcher(inner)
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = wrapped.dispatch(event)

    assert wrapped.last_result is result
    assert result.skipped is False


def test_skip_tracking_dispatcher_logs_warning_on_partial_skip(
    store: SQLiteIdMappingStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.upsert(
        IdMapping(
            notion_key="CLI-001",
            db_key="client_master",
            kintone_id="1001",
            zoho_id="zoho-1",
            spreadsheet_row=5,
            last_synced_at=NOW - timedelta(days=1),
        )
    )

    class _SkippingTarget:
        tool = Tool.KINTONE

        def get_record(self, external_id: str) -> dict[str, Any] | None:
            return None

        def upsert_record(self, external_id, properties):
            return None  # 常にスキップ

        def delete_record(self, external_id: str) -> None:
            pass

    inner = Dispatcher(store, {Tool.KINTONE: _SkippingTarget()})
    wrapped = SkipTrackingDispatcher(inner)
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    with caplog.at_level("WARNING"):
        result = wrapped.dispatch(event)

    assert result.has_partial_skips is True
    assert any(
        "partially skipped" in r.getMessage() and "CLI-001" in r.getMessage()
        for r in caplog.records
    )


# --- 永続化されないIDマッピングストアへの警告（shirokuma-secレビュー） ------------------------


def test_warn_if_id_mapping_store_not_persistent_logs_for_tmp_path(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        _warn_if_id_mapping_store_not_persistent("/tmp/sync_id_mapping.db")

    assert any("/tmp/sync_id_mapping.db" in r.getMessage() for r in caplog.records)


def test_warn_if_id_mapping_store_not_persistent_only_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        _warn_if_id_mapping_store_not_persistent("/tmp/sync_id_mapping.db")
        _warn_if_id_mapping_store_not_persistent("/tmp/sync_id_mapping.db")

    matching = [r for r in caplog.records if "/tmp/sync_id_mapping.db" in r.getMessage()]
    assert len(matching) == 1


def test_warn_if_id_mapping_store_not_persistent_does_not_log_for_persistent_path(
    caplog: pytest.LogCaptureFixture, tmp_path: Any
) -> None:
    persistent_path = str(tmp_path / "sync_id_mapping.db")

    with caplog.at_level("WARNING"):
        _warn_if_id_mapping_store_not_persistent(persistent_path)

    assert not any(persistent_path in r.getMessage() for r in caplog.records)


def test_build_id_mapping_store_warns_when_path_defaults_to_tmp(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SYNC_ID_MAPPING_DB_PATH", raising=False)

    with caplog.at_level("WARNING"):
        store = build_id_mapping_store()
    store.close()

    assert any("/tmp" in r.getMessage() for r in caplog.records)


# --- build_id_mapping_store: SYNC_ID_MAPPING_BACKEND=notion ------------------------------------


def test_build_id_mapping_store_returns_notion_backed_store_when_backend_is_notion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNC_ID_MAPPING_BACKEND", "notion")
    monkeypatch.setenv("SYNC_ID_MAPPING_NOTION_API_KEY", "secret-mapping-key")

    store = build_id_mapping_store()

    assert isinstance(store, NotionIdMappingStore)


def test_build_id_mapping_store_notion_backend_does_not_warn_about_persistence(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SYNC_ID_MAPPING_BACKEND", "notion")
    monkeypatch.setenv("SYNC_ID_MAPPING_NOTION_API_KEY", "secret-mapping-key")
    monkeypatch.delenv("SYNC_ID_MAPPING_DB_PATH", raising=False)

    with caplog.at_level("WARNING"):
        build_id_mapping_store()

    assert not any("/tmp" in r.getMessage() for r in caplog.records)


def test_build_id_mapping_store_defaults_to_sqlite_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SYNC_ID_MAPPING_BACKEND", raising=False)
    monkeypatch.setenv("SYNC_ID_MAPPING_DB_PATH", ":memory:")

    store = build_id_mapping_store()
    store.close()

    assert isinstance(store, SQLiteIdMappingStore)


def test_build_id_mapping_store_notion_backend_uses_interactive_rate_limit_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcherの同期的なWebhook処理経路から呼ばれるため、バルク移行向けの
    DEFAULT_MAX_RATE_LIMIT_RETRIES(30)ではなく、dashboard_service.py/task_service.pyと
    同じINTERACTIVE_MAX_RATE_LIMIT_RETRIES(小さい方)を使うこと（shirokuma-secレビューBLOCKER対応）。
    """
    monkeypatch.setenv("SYNC_ID_MAPPING_BACKEND", "notion")
    monkeypatch.setenv("SYNC_ID_MAPPING_NOTION_API_KEY", "secret-mapping-key")

    store = build_id_mapping_store()

    assert isinstance(store, NotionIdMappingStore)
    assert store._max_rate_limit_retries == INTERACTIVE_MAX_RATE_LIMIT_RETRIES  # noqa: SLF001
