from __future__ import annotations

import pytest

from src.gmail_sync.gmail_client import (
    GmailApiError,
    HistoryIdExpiredError,
    list_history,
    list_messages_page,
    watch_mailbox,
)

_ACCESS_TOKEN = "access-token"
_BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"


def test_watch_mailbox_returns_history_id_and_expiration(requests_mock) -> None:
    requests_mock.post(
        f"{_BASE_URL}/watch",
        json={"historyId": "12345", "expiration": "1755600000000"},
    )

    result = watch_mailbox(_ACCESS_TOKEN, "projects/test/topics/gmail-notifications")

    assert result == {"historyId": "12345", "expiration": "1755600000000"}
    request = requests_mock.request_history[0]
    assert request.json() == {
        "topicName": "projects/test/topics/gmail-notifications",
        "labelIds": ["INBOX"],
    }


def test_watch_mailbox_raises_on_error(requests_mock) -> None:
    requests_mock.post(f"{_BASE_URL}/watch", status_code=403, json={"error": "forbidden"})

    with pytest.raises(GmailApiError):
        watch_mailbox(_ACCESS_TOKEN, "projects/test/topics/gmail-notifications")


def test_list_history_returns_added_message_ids_and_history_id(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/history",
        json={
            "history": [
                {"messagesAdded": [{"message": {"id": "msg1"}}]},
                {"messagesAdded": [{"message": {"id": "msg2"}}, {"message": {"id": "msg3"}}]},
            ],
            "historyId": "5000",
        },
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result.message_ids == ["msg1", "msg2", "msg3"]
    assert result.history_id == "5000"


def test_list_history_follows_pagination_and_uses_last_page_history_id(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/history",
        [
            {
                "json": {
                    "history": [{"messagesAdded": [{"message": {"id": "msg1"}}]}],
                    "nextPageToken": "page2",
                    "historyId": "4000",
                }
            },
            {
                "json": {
                    "history": [{"messagesAdded": [{"message": {"id": "msg2"}}]}],
                    "historyId": "5000",
                }
            },
        ],
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result.message_ids == ["msg1", "msg2"]
    # 最後のページの値を採用する(shirokuma-secレビューWARN対応: レスポンス自体に含まれる
    # historyIdを使うことで、list_history()完了後に別途get_profile()を呼ぶ場合に生じる
    # レース(その間の新着メールのhistoryIdを見逃す)を避ける)。
    assert result.history_id == "5000"
    assert requests_mock.request_history[1].qs["pagetoken"] == ["page2"]


def test_list_history_returns_none_history_id_when_absent_from_response(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/history",
        json={"history": [{"messagesAdded": [{"message": {"id": "msg1"}}]}]},
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result.message_ids == ["msg1"]
    assert result.history_id is None


def test_list_history_raises_history_id_expired_on_404(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/history", status_code=404, json={"error": "not found"})

    with pytest.raises(HistoryIdExpiredError):
        list_history(_ACCESS_TOKEN, "too-old")


# --- list_messages_page（過去分の取り込み用、2026-09-03） -------------------------------------


def test_list_messages_page_returns_ids_and_next_page_token(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/messages",
        json={"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "tok-2"},
    )

    page = list_messages_page(_ACCESS_TOKEN, query="newer_than:365d")

    assert [ref.id for ref in page.refs] == ["m1", "m2"]
    assert page.next_page_token == "tok-2"
    request = requests_mock.request_history[0]
    assert request.qs["q"] == ["newer_than:365d"]
    assert "pagetoken" not in request.qs


def test_list_messages_page_sends_page_token_when_given(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/messages", json={"messages": [], "nextPageToken": None})

    page = list_messages_page(_ACCESS_TOKEN, query="q", page_token="tok-2")

    assert page.refs == []
    assert page.next_page_token is None
    assert requests_mock.request_history[0].qs["pagetoken"] == ["tok-2"]


def test_list_messages_page_returns_empty_on_no_results(requests_mock) -> None:
    """Gmailはヒット0件のとき`messages`キー自体を返さない。"""
    requests_mock.get(f"{_BASE_URL}/messages", json={})

    page = list_messages_page(_ACCESS_TOKEN, query="q")

    assert page.refs == []
    assert page.next_page_token is None


def test_list_messages_page_caps_max_results_at_the_api_limit(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/messages", json={"messages": []})

    list_messages_page(_ACCESS_TOKEN, query="q", max_results=100000)

    assert requests_mock.request_history[0].qs["maxresults"] == ["500"]


def test_list_messages_page_raises_on_error(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/messages", status_code=403, json={"error": "forbidden"})

    with pytest.raises(GmailApiError):
        list_messages_page(_ACCESS_TOKEN, query="q")
