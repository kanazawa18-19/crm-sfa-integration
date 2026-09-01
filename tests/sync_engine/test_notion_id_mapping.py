"""NotionIdMappingStoreの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.id_mapping import ConflictError, DuplicateExternalIdError, IdMapping
from src.sync_engine.notion_id_mapping import NotionIdMappingStore, NotionIdMappingStoreApiError

DATABASE_ID = "3b9d8ea8-d4f3-8059-8b04-ee5308d2cbf0"
QUERY_URL = f"https://api.notion.com/v1/databases/{DATABASE_ID}/query"
PAGES_URL = "https://api.notion.com/v1/pages"


@pytest.fixture
def store() -> NotionIdMappingStore:
    return NotionIdMappingStore(DATABASE_ID, api_key="secret-mapping-key")


def _page(
    page_id: str,
    *,
    notion_key: str,
    db_key: str | None = "client_master",
    kintone_id: str | None = None,
    zoho_id: str | None = None,
    spreadsheet_row: int | None = None,
    last_synced_at: str | None = None,
) -> dict:
    return {
        "id": page_id,
        "properties": {
            "notion_key": {"type": "title", "title": [{"plain_text": notion_key}]},
            "db_key": {"type": "select", "select": ({"name": db_key} if db_key else None)},
            "kintone_id": {
                "type": "rich_text",
                "rich_text": ([{"plain_text": kintone_id}] if kintone_id else []),
            },
            "zoho_id": {
                "type": "rich_text",
                "rich_text": ([{"plain_text": zoho_id}] if zoho_id else []),
            },
            "spreadsheet_row": {"type": "number", "number": spreadsheet_row},
            "last_synced_at": {
                "type": "date",
                "date": ({"start": last_synced_at} if last_synced_at else None),
            },
        },
    }


def _empty_query_response() -> dict:
    return {"results": [], "has_more": False, "next_cursor": None}


def _query_response(pages: list[dict]) -> dict:
    return {"results": pages, "has_more": False, "next_cursor": None}


# --- 認証情報未設定時のエラー -----------------------------------------------------------------


def test_raises_value_error_when_api_key_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNC_ID_MAPPING_NOTION_API_KEY", raising=False)

    with pytest.raises(ValueError, match="SYNC_ID_MAPPING_NOTION_API_KEY"):
        NotionIdMappingStore(DATABASE_ID)


def test_uses_default_database_id_when_not_specified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SYNC_ID_MAPPING_NOTION_DATABASE_ID", raising=False)

    s = NotionIdMappingStore(api_key="secret-mapping-key")

    assert s._database_id == DATABASE_ID  # noqa: SLF001 (テストのため内部状態を直接確認)


def test_uses_database_id_env_var_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_ID_MAPPING_NOTION_DATABASE_ID", "other-db-id")

    s = NotionIdMappingStore(api_key="secret-mapping-key")

    assert s._database_id == "other-db-id"  # noqa: SLF001


def test_warns_when_api_key_matches_content_sync_notion_api_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """専用トークンを使う目的（レート制限の枠を実データ同期と奪い合わないこと）が達成
    できていない誤設定（同一トークン使い回し）に気づけるよう警告を出すこと
    （shirokuma-secレビューWARN対応。起動はブロックしない）。"""
    monkeypatch.setenv("NOTION_API_KEY", "shared-secret")

    with caplog.at_level("WARNING"):
        NotionIdMappingStore(DATABASE_ID, api_key="shared-secret")

    assert any(
        "SYNC_ID_MAPPING_NOTION_API_KEY" in r.getMessage() and "NOTION_API_KEY" in r.getMessage()
        for r in caplog.records
    )


def test_does_not_warn_when_api_keys_differ(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("NOTION_API_KEY", "content-sync-secret")

    with caplog.at_level("WARNING"):
        NotionIdMappingStore(DATABASE_ID, api_key="secret-mapping-key")

    assert not any("NOTION_API_KEY" in r.getMessage() for r in caplog.records)


def test_does_not_warn_when_content_sync_notion_api_key_not_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)

    with caplog.at_level("WARNING"):
        NotionIdMappingStore(DATABASE_ID, api_key="secret-mapping-key")

    assert not any("NOTION_API_KEY" in r.getMessage() for r in caplog.records)


# --- get -----------------------------------------------------------------------------------


def test_get_returns_none_when_not_found(requests_mock, store: NotionIdMappingStore) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())

    assert store.get("CLI-999") is None


def test_get_returns_mapping_when_found(requests_mock, store: NotionIdMappingStore) -> None:
    page = _page(
        "page-1",
        notion_key="CLI-001",
        db_key="client_master",
        kintone_id="1001",
        zoho_id="zoho-abc",
        spreadsheet_row=5,
        last_synced_at="2026-08-01T12:00:00.000+00:00",
    )
    requests_mock.post(QUERY_URL, json=_query_response([page]))

    result = store.get("CLI-001")

    assert result == IdMapping(
        notion_key="CLI-001",
        db_key="client_master",
        kintone_id="1001",
        zoho_id="zoho-abc",
        spreadsheet_row=5,
        last_synced_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    sent_body = requests_mock.last_request.json()
    assert sent_body["filter"] == {"property": "notion_key", "title": {"equals": "CLI-001"}}


def test_get_raises_notion_api_error_on_5xx(
    requests_mock, store: NotionIdMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(QUERY_URL, status_code=500, json={"message": "internal error"})

    with pytest.raises(NotionIdMappingStoreApiError):
        store.get("CLI-001")


# --- upsert ----------------------------------------------------------------------------------


def test_upsert_creates_new_page_when_not_found(
    requests_mock, store: NotionIdMappingStore
) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())
    requests_mock.post(PAGES_URL, json={"id": "new-page-id"})

    store.upsert(
        IdMapping(
            notion_key="CLI-001",
            db_key="client_master",
            kintone_id="1001",
            last_synced_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        )
    )

    create_request = next(
        r for r in requests_mock.request_history if r.method == "POST" and r.url == PAGES_URL
    )
    body = create_request.json()
    assert body["parent"] == {"database_id": DATABASE_ID}
    assert body["properties"]["notion_key"] == {
        "title": [{"type": "text", "text": {"content": "CLI-001"}}]
    }
    assert body["properties"]["db_key"] == {"select": {"name": "client_master"}}
    assert body["properties"]["kintone_id"] == {
        "rich_text": [{"type": "text", "text": {"content": "1001"}}]
    }


def test_upsert_updates_existing_page_when_found(
    requests_mock, store: NotionIdMappingStore
) -> None:
    existing = _page("page-1", notion_key="CLI-001", kintone_id="1001")
    requests_mock.post(QUERY_URL, json=_query_response([existing]))
    requests_mock.patch(f"{PAGES_URL}/page-1", json={"id": "page-1"})

    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="9999"))

    patch_request = next(r for r in requests_mock.request_history if r.method == "PATCH")
    assert patch_request.url == f"{PAGES_URL}/page-1"
    body = patch_request.json()
    assert body["properties"]["kintone_id"] == {
        "rich_text": [{"type": "text", "text": {"content": "9999"}}]
    }
    assert "parent" not in body


def test_upsert_new_record_with_expected_none_succeeds(
    requests_mock, store: NotionIdMappingStore
) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())
    requests_mock.post(PAGES_URL, json={"id": "new-page-id"})

    store.upsert(
        IdMapping(notion_key="CLI-001", db_key="client_master"),
        expected_last_synced_at=None,
    )

    assert requests_mock.call_count == 2  # query + create


def test_upsert_with_matching_expected_last_synced_at_succeeds(
    requests_mock, store: NotionIdMappingStore
) -> None:
    synced_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    existing = _page(
        "page-1",
        notion_key="CLI-001",
        last_synced_at="2026-08-01T12:00:00+00:00",
    )
    requests_mock.post(QUERY_URL, json=_query_response([existing]))
    requests_mock.patch(f"{PAGES_URL}/page-1", json={"id": "page-1"})

    store.upsert(
        IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="9999"),
        expected_last_synced_at=synced_at,
    )

    assert any(r.method == "PATCH" for r in requests_mock.request_history)


def test_upsert_with_stale_expected_last_synced_at_raises_conflict(
    requests_mock, store: NotionIdMappingStore
) -> None:
    existing = _page(
        "page-1",
        notion_key="CLI-001",
        last_synced_at="2026-08-01T12:00:00+00:00",
    )
    requests_mock.post(QUERY_URL, json=_query_response([existing]))

    stale = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ConflictError):
        store.upsert(
            IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="9999"),
            expected_last_synced_at=stale,
        )

    assert not any(r.method == "PATCH" for r in requests_mock.request_history)


def test_upsert_rejects_duplicate_external_id(
    requests_mock, store: NotionIdMappingStore
) -> None:
    """kintone_idが既に別のnotion_keyへ紐づいている場合、書き込まずDuplicateExternalIdErrorを送出する。"""
    existing_page = _page("page-1", notion_key="CLI-001", kintone_id="1001")

    def query_matcher(request, context):
        filt = request.json()["filter"]
        if filt == {
            "and": [
                {"property": "kintone_id", "rich_text": {"equals": "1001"}},
                {"property": "db_key", "select": {"equals": "client_master"}},
            ]
        }:
            return {"results": [existing_page], "has_more": False, "next_cursor": None}
        return _empty_query_response()

    requests_mock.post(QUERY_URL, json=query_matcher)

    with pytest.raises(DuplicateExternalIdError):
        store.upsert(IdMapping(notion_key="CLI-002", db_key="client_master", kintone_id="1001"))

    assert not any(r.method in ("POST", "PATCH") and r.url != QUERY_URL for r in requests_mock.request_history)


def test_upsert_same_notion_key_reusing_own_external_id_is_allowed(
    requests_mock, store: NotionIdMappingStore
) -> None:
    """同一notion_keyに対する再upsert（自分自身の外部IDそのまま）は重複エラーにならない。"""
    existing_page = _page("page-1", notion_key="CLI-001", kintone_id="1001")
    requests_mock.post(QUERY_URL, json=_query_response([existing_page]))
    requests_mock.patch(f"{PAGES_URL}/page-1", json={"id": "page-1"})

    store.upsert(
        IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="1001", zoho_id="zoho-new")
    )

    assert any(r.method == "PATCH" for r in requests_mock.request_history)


# --- delete ----------------------------------------------------------------------------------


def test_delete_archives_existing_page(requests_mock, store: NotionIdMappingStore) -> None:
    existing = _page("page-1", notion_key="CLI-001")
    requests_mock.post(QUERY_URL, json=_query_response([existing]))
    requests_mock.patch(f"{PAGES_URL}/page-1", json={"id": "page-1"})

    store.delete("CLI-001")

    patch_request = next(r for r in requests_mock.request_history if r.method == "PATCH")
    assert patch_request.url == f"{PAGES_URL}/page-1"
    assert patch_request.json() == {"archived": True}


def test_delete_nonexistent_key_is_noop(requests_mock, store: NotionIdMappingStore) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())

    store.delete("CLI-does-not-exist")

    assert not any(r.method == "PATCH" for r in requests_mock.request_history)


# --- find_by_external_id ----------------------------------------------------------------------


def test_find_by_external_id_kintone(requests_mock, store: NotionIdMappingStore) -> None:
    page = _page("page-1", notion_key="CLI-001", kintone_id="1001")
    requests_mock.post(QUERY_URL, json=_query_response([page]))

    result = store.find_by_external_id(Tool.KINTONE, "1001", db_key="client_master")

    assert result is not None
    assert result.notion_key == "CLI-001"
    sent_body = requests_mock.last_request.json()
    # 2026-08-14、shirokuma-secレビューBLOCKER対応: db_keyも必ず絞り込む複合フィルターになる
    # （外部IDだけの検索は、kintoneのレコード番号がアプリ単位で独立採番されているため、
    # 別db_keyの同番号レコードと衝突しうる）。
    assert sent_body["filter"] == {
        "and": [
            {"property": "kintone_id", "rich_text": {"equals": "1001"}},
            {"property": "db_key", "select": {"equals": "client_master"}},
        ]
    }


def test_find_by_external_id_zoho(requests_mock, store: NotionIdMappingStore) -> None:
    page = _page("page-1", notion_key="CLI-001", zoho_id="zoho-abc")
    requests_mock.post(QUERY_URL, json=_query_response([page]))

    result = store.find_by_external_id(Tool.ZOHO, "zoho-abc", db_key="client_master")

    assert result is not None
    assert result.notion_key == "CLI-001"
    sent_body = requests_mock.last_request.json()
    assert sent_body["filter"] == {
        "and": [
            {"property": "zoho_id", "rich_text": {"equals": "zoho-abc"}},
            {"property": "db_key", "select": {"equals": "client_master"}},
        ]
    }


def test_find_by_external_id_spreadsheet_row(requests_mock, store: NotionIdMappingStore) -> None:
    page = _page("page-1", notion_key="CLI-001", spreadsheet_row=42)
    requests_mock.post(QUERY_URL, json=_query_response([page]))

    result = store.find_by_external_id(Tool.SPREADSHEET, "42", db_key="client_master")

    assert result is not None
    assert result.notion_key == "CLI-001"
    sent_body = requests_mock.last_request.json()
    assert sent_body["filter"] == {
        "and": [
            {"property": "spreadsheet_row", "number": {"equals": 42}},
            {"property": "db_key", "select": {"equals": "client_master"}},
        ]
    }


def test_find_by_external_id_returns_none_when_not_found(
    requests_mock, store: NotionIdMappingStore
) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())

    assert store.find_by_external_id(Tool.KINTONE, "no-such-id", db_key="client_master") is None


def test_find_by_external_id_unsupported_tool_raises(store: NotionIdMappingStore) -> None:
    with pytest.raises(ValueError):
        store.find_by_external_id(Tool.NOTION, "CLI-001", db_key="client_master")


# --- update_last_synced_at --------------------------------------------------------------------


def test_update_last_synced_at_success(requests_mock, store: NotionIdMappingStore) -> None:
    existing = _page("page-1", notion_key="CLI-001")
    requests_mock.post(QUERY_URL, json=_query_response([existing]))
    requests_mock.patch(f"{PAGES_URL}/page-1", json={"id": "page-1"})
    synced_at = datetime(2026, 8, 5, 9, 30, 0, tzinfo=timezone.utc)

    store.update_last_synced_at("CLI-001", synced_at)

    patch_request = next(r for r in requests_mock.request_history if r.method == "PATCH")
    assert patch_request.json() == {
        "properties": {"last_synced_at": {"date": {"start": synced_at.isoformat()}}}
    }


def test_update_last_synced_at_unknown_key_raises(
    requests_mock, store: NotionIdMappingStore
) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())

    with pytest.raises(KeyError):
        store.update_last_synced_at("CLI-does-not-exist", datetime.now(timezone.utc))


# --- list_by_db (ページング) -------------------------------------------------------------------


def test_list_by_db_returns_all_results(requests_mock, store: NotionIdMappingStore) -> None:
    pages = [
        _page("page-1", notion_key="CLI-001", db_key="client_master"),
        _page("page-2", notion_key="CLI-002", db_key="client_master"),
    ]
    requests_mock.post(QUERY_URL, json=_query_response(pages))

    results = store.list_by_db("client_master")

    assert {r.notion_key for r in results} == {"CLI-001", "CLI-002"}
    sent_body = requests_mock.last_request.json()
    assert sent_body["filter"] == {"property": "db_key", "select": {"equals": "client_master"}}


def test_list_by_db_paginates_across_multiple_pages(
    requests_mock, store: NotionIdMappingStore
) -> None:
    requests_mock.post(
        QUERY_URL,
        [
            {
                "json": {
                    "results": [_page("page-1", notion_key="CLI-001")],
                    "has_more": True,
                    "next_cursor": "cursor-abc",
                },
                "status_code": 200,
            },
            {
                "json": {
                    "results": [_page("page-2", notion_key="CLI-002")],
                    "has_more": False,
                    "next_cursor": None,
                },
                "status_code": 200,
            },
        ],
    )

    results = store.list_by_db("client_master")

    assert {r.notion_key for r in results} == {"CLI-001", "CLI-002"}
    assert requests_mock.call_count == 2
    second_request_body = requests_mock.request_history[1].json()
    assert second_request_body["start_cursor"] == "cursor-abc"


def test_list_by_db_returns_empty_for_unknown_db(
    requests_mock, store: NotionIdMappingStore
) -> None:
    requests_mock.post(QUERY_URL, json=_empty_query_response())

    assert store.list_by_db("no-such-db") == []


def test_list_by_db_logs_warning_when_has_more_true_but_next_cursor_missing(
    requests_mock, store: NotionIdMappingStore, caplog: pytest.LogCaptureFixture
) -> None:
    """has_more=Trueかつnext_cursorが空という契約上起きないはずのレスポンスが返っても
    無限ループにならず打ち切ること、かつHttpNotionClient.query_all_pages()と同様に
    warningログでこの異常を可視化すること（shirokuma-secレビューWARN対応）。"""
    requests_mock.post(
        QUERY_URL,
        json={
            "results": [_page("page-1", notion_key="CLI-001")],
            "has_more": True,
            "next_cursor": None,
        },
    )

    with caplog.at_level("WARNING"):
        results = store.list_by_db("client_master")

    assert {r.notion_key for r in results} == {"CLI-001"}
    assert requests_mock.call_count == 1
    # メッセージは日本語の共通ページング処理（_notion_paging.py）が出す。
    assert any("next_cursor が空" in r.getMessage() for r in caplog.records)
