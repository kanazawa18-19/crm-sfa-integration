"""RevenueTargetSettingsStore（事業計画スプレッドシートへのポインタ設定）の単体テスト。

実HTTP通信はrequests_mockでモックする（tests/sync_engine/test_notion_id_mapping.pyと
同じ流儀）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.reports.revenue_target_sheet import RevenueTargetSheetPointer
from src.reports.revenue_target_settings import (
    RevenueTargetSettingsRecord,
    RevenueTargetSettingsStore,
    RevenueTargetSettingsStoreApiError,
    build_revenue_target_settings_store,
)

DATABASE_ID = "settings-db-id"
QUERY_URL = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
PAGES_URL = "https://api.notion.com/v1/pages"


@pytest.fixture
def store() -> RevenueTargetSettingsStore:
    return RevenueTargetSettingsStore(DATABASE_ID, api_key="secret-settings-key")


def _page(
    page_id: str,
    *,
    spreadsheet_id: str = "sheet-abc",
    mrr_sheet_name: str | None = "✳︎営業部事業計画（月額ver）",
    unit_count_sheet_name: str | None = "✳︎販売計画",
    updated_at: str | None = "2026-08-13T09:00:00.000+00:00",
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "key": {"type": "title", "title": [{"plain_text": "revenue_target_sheet_pointer"}]},
            "spreadsheet_id": {
                "type": "rich_text",
                "rich_text": [{"plain_text": spreadsheet_id}] if spreadsheet_id else [],
            },
            "mrr_sheet_name": {
                "type": "rich_text",
                "rich_text": [{"plain_text": mrr_sheet_name}] if mrr_sheet_name else [],
            },
            "unit_count_sheet_name": {
                "type": "rich_text",
                "rich_text": [{"plain_text": unit_count_sheet_name}] if unit_count_sheet_name else [],
            },
            "updated_at": {
                "type": "date",
                "date": ({"start": updated_at} if updated_at else None),
            },
        },
    }


def _empty_query_response() -> dict:
    return {"results": [], "has_more": False, "next_cursor": None}


def _query_response(pages: list[dict]) -> dict:
    return {"results": pages, "has_more": False, "next_cursor": None}


# --- 認証情報未設定時の挙動 ---------------------------------------------------------------------


def test_raises_value_error_when_database_id_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID", raising=False)

    with pytest.raises(ValueError, match="REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID"):
        RevenueTargetSettingsStore(api_key="secret-settings-key")


def test_raises_value_error_when_api_key_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVENUE_TARGET_SETTINGS_NOTION_API_KEY", raising=False)
    monkeypatch.delenv("SYNC_ID_MAPPING_NOTION_API_KEY", raising=False)

    with pytest.raises(ValueError, match="REVENUE_TARGET_SETTINGS_NOTION_API_KEY"):
        RevenueTargetSettingsStore(DATABASE_ID)


def test_falls_back_to_sync_id_mapping_notion_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """専用トークン未設定時、IDマッピング専用トークン（低ボリューム）を再利用する
    （モジュールdocstring参照）。"""
    monkeypatch.delenv("REVENUE_TARGET_SETTINGS_NOTION_API_KEY", raising=False)
    monkeypatch.setenv("SYNC_ID_MAPPING_NOTION_API_KEY", "id-mapping-key")

    s = RevenueTargetSettingsStore(DATABASE_ID)

    assert s._api_key == "id-mapping-key"  # noqa: SLF001


def test_build_revenue_target_settings_store_returns_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID", raising=False)
    monkeypatch.delenv("REVENUE_TARGET_SETTINGS_NOTION_API_KEY", raising=False)
    monkeypatch.delenv("SYNC_ID_MAPPING_NOTION_API_KEY", raising=False)

    assert build_revenue_target_settings_store() is None


def test_build_revenue_target_settings_store_returns_store_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID", DATABASE_ID)
    monkeypatch.setenv("REVENUE_TARGET_SETTINGS_NOTION_API_KEY", "secret-settings-key")

    result = build_revenue_target_settings_store()

    assert isinstance(result, RevenueTargetSettingsStore)


# --- get -----------------------------------------------------------------------------------


def test_get_returns_none_when_not_found(requests_mock, store: RevenueTargetSettingsStore) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())

    assert store.get() is None


def test_get_returns_record_when_found(requests_mock, store: RevenueTargetSettingsStore) -> None:
    page = _page(
        "page-1",
        spreadsheet_id="sheet-abc",
        mrr_sheet_name="✳︎営業部事業計画（月額ver）",
        unit_count_sheet_name="✳︎販売計画",
        updated_at="2026-08-13T09:00:00.000+00:00",
    )
    requests_mock.post(QUERY_URL, json=_query_response([page]))

    result = store.get()

    assert result == RevenueTargetSettingsRecord(
        pointer=RevenueTargetSheetPointer(
            spreadsheet_id="sheet-abc",
            mrr_sheet_name="✳︎営業部事業計画（月額ver）",
            unit_count_sheet_name="✳︎販売計画",
        ),
        updated_at=datetime(2026, 8, 13, 9, 0, 0, tzinfo=timezone.utc),
    )
    sent_body = requests_mock.last_request.json()
    assert sent_body["filter"] == {
        "property": "key",
        "title": {"equals": "revenue_target_sheet_pointer"},
    }


def test_get_treats_unset_sheet_names_as_none(
    requests_mock, store: RevenueTargetSettingsStore
) -> None:
    """MRRシート・販売数シートのどちらか一方だけ運用しているケースを許容する
    （RevenueTargetSheetPointerの既存仕様通り）。"""
    page = _page("page-1", mrr_sheet_name=None, unit_count_sheet_name="✳︎販売計画")
    requests_mock.post(QUERY_URL, json=_query_response([page]))

    result = store.get()

    assert result is not None
    assert result.pointer.mrr_sheet_name is None
    assert result.pointer.unit_count_sheet_name == "✳︎販売計画"


def test_get_raises_api_error_on_5xx(
    requests_mock, store: RevenueTargetSettingsStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(QUERY_URL, status_code=500, json={"message": "internal error"})

    with pytest.raises(RevenueTargetSettingsStoreApiError):
        store.get()


# --- upsert ----------------------------------------------------------------------------------


def test_upsert_creates_new_page_when_not_found(
    requests_mock, store: RevenueTargetSettingsStore
) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())
    requests_mock.post(PAGES_URL, json={"id": "new-page-id"})

    pointer = RevenueTargetSheetPointer(
        spreadsheet_id="sheet-new", mrr_sheet_name="MRRシート", unit_count_sheet_name="販売数シート"
    )
    fixed_now = datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc)

    record = store.upsert(pointer, updated_at=fixed_now)

    assert record == RevenueTargetSettingsRecord(pointer=pointer, updated_at=fixed_now)
    create_request = next(
        r for r in requests_mock.request_history if r.method == "POST" and r.url == PAGES_URL
    )
    body = create_request.json()
    assert body["parent"] == {"database_id": DATABASE_ID}
    assert body["properties"]["key"] == {
        "title": [{"type": "text", "text": {"content": "revenue_target_sheet_pointer"}}]
    }
    assert body["properties"]["spreadsheet_id"] == {
        "rich_text": [{"type": "text", "text": {"content": "sheet-new"}}]
    }
    assert body["properties"]["updated_at"] == {"date": {"start": fixed_now.isoformat()}}


def test_upsert_updates_existing_page_when_found(
    requests_mock, store: RevenueTargetSettingsStore
) -> None:
    existing = _page("page-1")
    requests_mock.post(QUERY_URL, json=_query_response([existing]))
    requests_mock.patch(f"{PAGES_URL}/page-1", json={"id": "page-1"})

    pointer = RevenueTargetSheetPointer(spreadsheet_id="sheet-updated")
    store.upsert(pointer, updated_at=datetime(2026, 8, 13, 10, 0, 0, tzinfo=timezone.utc))

    patch_request = next(r for r in requests_mock.request_history if r.method == "PATCH")
    assert patch_request.url == f"{PAGES_URL}/page-1"
    body = patch_request.json()
    assert body["properties"]["spreadsheet_id"] == {
        "rich_text": [{"type": "text", "text": {"content": "sheet-updated"}}]
    }
    assert "parent" not in body


def test_upsert_allows_omitting_optional_sheet_names(
    requests_mock, store: RevenueTargetSettingsStore
) -> None:
    """mrr_sheet_name／unit_count_sheet_nameのどちらか一方だけの保存も許容する。"""
    requests_mock.post(QUERY_URL, json=_empty_query_response())
    requests_mock.post(PAGES_URL, json={"id": "new-page-id"})

    pointer = RevenueTargetSheetPointer(spreadsheet_id="sheet-abc", unit_count_sheet_name="販売数シート")
    store.upsert(pointer)

    create_request = next(
        r for r in requests_mock.request_history if r.method == "POST" and r.url == PAGES_URL
    )
    body = create_request.json()
    assert body["properties"]["mrr_sheet_name"] == {"rich_text": []}
    assert body["properties"]["unit_count_sheet_name"] == {
        "rich_text": [{"type": "text", "text": {"content": "販売数シート"}}]
    }
