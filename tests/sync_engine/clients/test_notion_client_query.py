"""HttpNotionClient.query_all_pages()のページング動作の単体テスト（requests_mockでモック）。"""

from __future__ import annotations

import pytest

from src.sync_engine.clients.notion_client import HttpNotionClient, NotionApiError

DB_KEY = "client_master"
DATABASE_ID = "26d6f1e2-1111-1111-1111-111111111111"


@pytest.fixture
def client() -> HttpNotionClient:
    return HttpNotionClient(DB_KEY, DATABASE_ID, api_key="secret-notion-key")


def test_query_all_pages_returns_single_page_results(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={
            "results": [{"id": "page-1", "properties": {}}, {"id": "page-2", "properties": {}}],
            "has_more": False,
            "next_cursor": None,
        },
    )

    pages = client.query_all_pages()

    assert [p["id"] for p in pages] == ["page-1", "page-2"]
    assert requests_mock.call_count == 1


def test_query_all_pages_follows_has_more_cursor(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        [
            {
                "json": {
                    "results": [{"id": "page-1", "properties": {}}],
                    "has_more": True,
                    "next_cursor": "cursor-abc",
                },
                "status_code": 200,
            },
            {
                "json": {
                    "results": [{"id": "page-2", "properties": {}}],
                    "has_more": False,
                    "next_cursor": None,
                },
                "status_code": 200,
            },
        ],
    )

    pages = client.query_all_pages()

    assert [p["id"] for p in pages] == ["page-1", "page-2"]
    assert requests_mock.call_count == 2
    second_request_body = requests_mock.request_history[1].json()
    assert second_request_body["start_cursor"] == "cursor-abc"


def test_query_all_pages_stops_when_has_more_true_but_next_cursor_missing(
    requests_mock, client: HttpNotionClient
) -> None:
    """has_more=Trueかつnext_cursorが空という契約上起きないはずのレスポンスが返っても、
    start_cursor=Noneに戻って最初からページングをやり直す無限ループにならないこと。"""
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={
            "results": [{"id": "page-1", "properties": {}}],
            "has_more": True,
            "next_cursor": None,
        },
    )

    pages = client.query_all_pages()

    assert [p["id"] for p in pages] == ["page-1"]
    assert requests_mock.call_count == 1


def test_query_all_pages_sends_page_size(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [], "has_more": False, "next_cursor": None},
    )

    client.query_all_pages(page_size=50)

    assert requests_mock.last_request.json()["page_size"] == 50


def test_query_all_pages_sends_bearer_token_and_notion_version_header(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [], "has_more": False, "next_cursor": None},
    )

    client.query_all_pages()

    sent_headers = requests_mock.last_request.headers
    assert sent_headers["Authorization"] == "Bearer secret-notion-key"
    assert sent_headers["Notion-Version"] == "2022-06-28"


def test_query_all_pages_raises_notion_api_error_on_5xx(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        status_code=500,
        json={"message": "internal error"},
    )

    with pytest.raises(NotionApiError):
        client.query_all_pages()
