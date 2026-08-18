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


def test_query_all_pages_sends_filter_when_given(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [], "has_more": False, "next_cursor": None},
    )
    filter_body = {"property": "メールアドレス", "email": {"equals": "yamada@example.com"}}

    client.query_all_pages(filter=filter_body)

    assert requests_mock.last_request.json()["filter"] == filter_body


def test_query_all_pages_omits_filter_key_when_not_given(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [], "has_more": False, "next_cursor": None},
    )

    client.query_all_pages()

    assert "filter" not in requests_mock.last_request.json()


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


# --- query_page（has_moreを追わない1回打ち切り版） ------------------------------------------


def test_query_page_returns_single_page_results(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={
            "results": [{"id": "page-1", "properties": {}}, {"id": "page-2", "properties": {}}],
            "has_more": True,
            "next_cursor": "cursor-abc",
        },
    )

    pages = client.query_page()

    assert [p["id"] for p in pages] == ["page-1", "page-2"]


def test_query_page_does_not_follow_has_more_cursor(
    requests_mock, client: HttpNotionClient
) -> None:
    """query_all_pagesと異なり、has_more=Trueでも1回のリクエストで打ち切ること。"""
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={
            "results": [{"id": "page-1", "properties": {}}],
            "has_more": True,
            "next_cursor": "cursor-abc",
        },
    )

    pages = client.query_page()

    assert [p["id"] for p in pages] == ["page-1"]
    assert requests_mock.call_count == 1


def test_query_page_sends_page_size(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [], "has_more": False, "next_cursor": None},
    )

    client.query_page(page_size=20)

    assert requests_mock.last_request.json()["page_size"] == 20


def test_query_page_sends_filter_when_given(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [], "has_more": False, "next_cursor": None},
    )
    filter_body = {"property": "取引先名", "title": {"contains": "サンプル"}}

    client.query_page(filter=filter_body)

    assert requests_mock.last_request.json()["filter"] == filter_body


def test_query_page_omits_filter_and_sorts_keys_when_not_given(
    requests_mock, client: HttpNotionClient
) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [], "has_more": False, "next_cursor": None},
    )

    client.query_page()

    sent_body = requests_mock.last_request.json()
    assert "filter" not in sent_body
    assert "sorts" not in sent_body


def test_query_page_sends_sorts_when_given(requests_mock, client: HttpNotionClient) -> None:
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        json={"results": [], "has_more": False, "next_cursor": None},
    )
    sorts_body = [{"property": "作成日時", "direction": "descending"}]

    client.query_page(sorts=sorts_body)

    assert requests_mock.last_request.json()["sorts"] == sorts_body


def test_query_page_raises_notion_api_error_on_5xx(
    requests_mock, client: HttpNotionClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(
        f"https://api.notion.com/v1/databases/{DATABASE_ID}/query",
        status_code=500,
        json={"message": "internal error"},
    )

    with pytest.raises(NotionApiError):
        client.query_page()
