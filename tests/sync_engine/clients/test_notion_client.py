"""HttpNotionClientの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

import pytest

from src.db_schema.base import PropertyType
from src.db_schema.registry import get_schema
from src.sync_engine.clients.notion_client import (
    HttpNotionClient,
    NotionApiError,
    build_notion_properties,
    build_notion_property_value,
)

DB_KEY = "client_master"
DATABASE_ID = "26d6f1e2-1111-1111-1111-111111111111"
PAGE_ID = "26d6f1e2-0000-0000-0000-000000000000"


@pytest.fixture
def client() -> HttpNotionClient:
    return HttpNotionClient(DB_KEY, DATABASE_ID, api_key="secret-notion-key")


# --- 認証情報未設定時のエラー -------------------------------------------------------------------


def test_raises_value_error_when_api_key_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)

    with pytest.raises(ValueError, match="NOTION_API_KEY"):
        HttpNotionClient(DB_KEY, DATABASE_ID)


# --- get_page ----------------------------------------------------------------------------


def test_get_page_returns_flat_properties_dict(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        json={
            "id": PAGE_ID,
            "properties": {
                "取引先ID": {"type": "title", "title": [{"plain_text": "CLI-001"}]},
                "取引先名": {"type": "rich_text", "rich_text": [{"plain_text": "株式会社サンプル"}]},
                "顧客種別": {"type": "select", "select": {"name": "ホテル・旅館"}},
                "営業ステータス": {"type": "status", "status": {"name": "商談中"}},
                "チェーン": {"type": "relation", "relation": [{"id": "chain-1"}]},
            },
        },
    )

    record = client.get_page(PAGE_ID)

    assert record == {
        "取引先ID": "CLI-001",
        "取引先名": "株式会社サンプル",
        "顧客種別": "ホテル・旅館",
        "営業ステータス": "商談中",
        "チェーン": ["chain-1"],
    }


def test_get_page_returns_none_on_404(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.get(f"https://api.notion.com/v1/pages/{PAGE_ID}", status_code=404)

    assert client.get_page(PAGE_ID) is None


def test_get_page_sends_bearer_token_and_notion_version_header(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID, "properties": {}}
    )

    client.get_page(PAGE_ID)

    sent_headers = requests_mock.last_request.headers
    assert sent_headers["Authorization"] == "Bearer secret-notion-key"
    assert sent_headers["Notion-Version"] == "2022-06-28"


def test_get_page_raises_notion_api_error_on_5xx(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=500,
        json={"message": "internal error"},
    )

    with pytest.raises(NotionApiError) as exc_info:
        client.get_page(PAGE_ID)
    assert exc_info.value.status_code == 500


# --- create_page ---------------------------------------------------------------------------


def test_create_page_sends_correct_body_and_returns_id(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.post("https://api.notion.com/v1/pages", json={"id": "new-page-id"})

    page_id = client.create_page({"取引先ID": "CLI-002", "顧客種別": "飲食"})

    assert page_id == "new-page-id"
    sent_body = requests_mock.last_request.json()
    assert sent_body["parent"] == {"database_id": DATABASE_ID}
    assert sent_body["properties"]["取引先ID"] == {
        "title": [{"type": "text", "text": {"content": "CLI-002"}}]
    }
    assert sent_body["properties"]["顧客種別"] == {"select": {"name": "飲食"}}


def test_create_page_does_not_retry_on_5xx(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WARN対応: 作成系（非冪等）操作はサーバー側で処理済みの可能性があるため、
    5xxでもリトライせず即座にエラーとして返す（重複ページ作成を避ける）。
    """
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(
        "https://api.notion.com/v1/pages", status_code=500, json={"message": "internal error"}
    )

    with pytest.raises(NotionApiError):
        client.create_page({"取引先ID": "CLI-002"})

    assert requests_mock.call_count == 1


# --- update_page ---------------------------------------------------------------------------


def test_update_page_sends_patch_with_properties(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.patch(f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID})

    client.update_page(PAGE_ID, {"取引先名": "更新後の名称"})

    sent_body = requests_mock.last_request.json()
    assert sent_body == {
        "properties": {
            "取引先名": {"rich_text": [{"type": "text", "text": {"content": "更新後の名称"}}]}
        }
    }
    assert "parent" not in sent_body


def test_update_page_raises_notion_api_error_on_400(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.patch(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        status_code=400,
        json={"message": "validation failed"},
    )

    with pytest.raises(NotionApiError):
        client.update_page(PAGE_ID, {"取引先名": "更新後の名称"})


# --- archive_page --------------------------------------------------------------------------


def test_archive_page_sends_patch_with_archived_true(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.patch(f"https://api.notion.com/v1/pages/{PAGE_ID}", json={"id": PAGE_ID})

    client.archive_page(PAGE_ID)

    assert requests_mock.last_request.json() == {"archived": True}


# --- タイムアウト・リトライ ------------------------------------------------------------------


def test_get_page_retries_on_429_then_succeeds(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(
        f"https://api.notion.com/v1/pages/{PAGE_ID}",
        [
            {"status_code": 429},
            {"json": {"id": PAGE_ID, "properties": {}}, "status_code": 200},
        ],
    )

    record = client.get_page(PAGE_ID)

    assert record == {}
    assert requests_mock.call_count == 2


# --- プロパティ形式の相互変換ロジック（内部値 -> Notion形式） --------------------------------


@pytest.mark.parametrize(
    ("property_type", "value", "expected"),
    [
        (PropertyType.TITLE, "サンプル", {"title": [{"type": "text", "text": {"content": "サンプル"}}]}),
        (PropertyType.TITLE, None, {"title": []}),
        (PropertyType.TEXT, "本文", {"rich_text": [{"type": "text", "text": {"content": "本文"}}]}),
        (PropertyType.SELECT, "A", {"select": {"name": "A"}}),
        (PropertyType.SELECT, None, {"select": None}),
        (PropertyType.STATUS, "提案中", {"status": {"name": "提案中"}}),
        (PropertyType.NUMBER, 500000, {"number": 500000}),
        (PropertyType.CURRENCY, 1000, {"number": 1000}),
        (PropertyType.DATE, "2026-08-05", {"date": {"start": "2026-08-05"}}),
        (PropertyType.DATE, None, {"date": None}),
        (PropertyType.EMAIL, "a@example.com", {"email": "a@example.com"}),
        (PropertyType.PHONE, "03-1234-5678", {"phone_number": "03-1234-5678"}),
        (PropertyType.URL, "https://example.com", {"url": "https://example.com"}),
        (PropertyType.CHECKBOX, True, {"checkbox": True}),
        (PropertyType.USER, ["user-1", "user-2"], {"people": [{"id": "user-1"}, {"id": "user-2"}]}),
        (PropertyType.USER, "user-1", {"people": [{"id": "user-1"}]}),
        (PropertyType.RELATION, ["rel-1"], {"relation": [{"id": "rel-1"}]}),
        (PropertyType.RELATION, None, {"relation": []}),
        (PropertyType.JSON_TEXT, '{"k": 1}', {"rich_text": [{"type": "text", "text": {"content": '{"k": 1}'}}]}),
    ],
)
def test_build_notion_property_value(property_type: PropertyType, value, expected) -> None:
    assert build_notion_property_value(property_type, value) == expected


def test_build_notion_property_value_rejects_unsupported_type() -> None:
    with pytest.raises(ValueError):
        build_notion_property_value("not_a_real_type", "x")  # type: ignore[arg-type]


def test_build_notion_properties_uses_schema_to_determine_type() -> None:
    schema = get_schema(DB_KEY)

    result = build_notion_properties({"取引先ID": "CLI-001", "顧客種別": "飲食"}, schema)

    assert result["取引先ID"] == {"title": [{"type": "text", "text": {"content": "CLI-001"}}]}
    assert result["顧客種別"] == {"select": {"name": "飲食"}}


def test_build_notion_properties_raises_key_error_for_unknown_property() -> None:
    schema = get_schema(DB_KEY)

    with pytest.raises(KeyError):
        build_notion_properties({"存在しないプロパティ": "x"}, schema)
