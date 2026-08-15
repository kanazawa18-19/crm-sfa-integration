from __future__ import annotations

import pytest

from src.gmail_sync.gmail_client import (
    GmailApiError,
    HistoryIdExpiredError,
    get_profile,
    list_history,
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


def test_list_history_returns_added_message_ids(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/history",
        json={
            "history": [
                {"messagesAdded": [{"message": {"id": "msg1"}}]},
                {"messagesAdded": [{"message": {"id": "msg2"}}, {"message": {"id": "msg3"}}]},
            ]
        },
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result == ["msg1", "msg2", "msg3"]


def test_list_history_follows_pagination(requests_mock) -> None:
    requests_mock.get(
        f"{_BASE_URL}/history",
        [
            {
                "json": {
                    "history": [{"messagesAdded": [{"message": {"id": "msg1"}}]}],
                    "nextPageToken": "page2",
                }
            },
            {"json": {"history": [{"messagesAdded": [{"message": {"id": "msg2"}}]}]}},
        ],
    )

    result = list_history(_ACCESS_TOKEN, "1000")

    assert result == ["msg1", "msg2"]
    assert requests_mock.request_history[1].qs["pagetoken"] == ["page2"]


def test_list_history_raises_history_id_expired_on_404(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/history", status_code=404, json={"error": "not found"})

    with pytest.raises(HistoryIdExpiredError):
        list_history(_ACCESS_TOKEN, "too-old")


def test_get_profile_returns_history_id(requests_mock) -> None:
    requests_mock.get(f"{_BASE_URL}/profile", json={"emailAddress": "rep@cnctor.jp", "historyId": "9999"})

    result = get_profile(_ACCESS_TOKEN)

    assert result["historyId"] == "9999"
