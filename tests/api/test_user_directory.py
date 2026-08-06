"""NotionUserDirectoryの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

import pytest

from src.api.user_directory import NotionUserDirectory, NotionUserDirectoryError


@pytest.fixture
def directory() -> NotionUserDirectory:
    return NotionUserDirectory(api_key="secret-notion-key")


# --- ページネーション ----------------------------------------------------------------------------


def test_resolve_follows_has_more_cursor_and_resolves_all_users(
    requests_mock, directory: NotionUserDirectory
) -> None:
    requests_mock.get(
        "https://api.notion.com/v1/users",
        [
            {
                "json": {
                    "results": [{"id": "user-1", "name": "田中太郎"}],
                    "has_more": True,
                    "next_cursor": "cursor-abc",
                },
                "status_code": 200,
            },
            {
                "json": {
                    "results": [{"id": "user-2", "name": "鈴木花子"}],
                    "has_more": False,
                    "next_cursor": None,
                },
                "status_code": 200,
            },
        ],
    )

    assert directory.resolve("user-1") == "田中太郎"
    assert directory.resolve("user-2") == "鈴木花子"
    assert requests_mock.call_count == 2
    second_request_qs = requests_mock.request_history[1].qs
    assert second_request_qs["start_cursor"] == ["cursor-abc"]


# --- インスタンス内キャッシュ ----------------------------------------------------------------------


def test_resolve_only_fetches_users_once(requests_mock, directory: NotionUserDirectory) -> None:
    requests_mock.get(
        "https://api.notion.com/v1/users",
        json={
            "results": [{"id": "user-1", "name": "田中太郎"}],
            "has_more": False,
            "next_cursor": None,
        },
    )

    directory.resolve("user-1")
    directory.resolve("user-1")

    assert requests_mock.call_count == 1


# --- フォールバック ------------------------------------------------------------------------------


def test_resolve_returns_user_id_as_is_when_not_found(
    requests_mock, directory: NotionUserDirectory
) -> None:
    requests_mock.get(
        "https://api.notion.com/v1/users",
        json={"results": [], "has_more": False, "next_cursor": None},
    )

    assert directory.resolve("unknown-user-id") == "unknown-user-id"


# --- resolve_many --------------------------------------------------------------------------------


def test_resolve_many_resolves_multiple_ids(
    requests_mock, directory: NotionUserDirectory
) -> None:
    requests_mock.get(
        "https://api.notion.com/v1/users",
        json={
            "results": [
                {"id": "user-1", "name": "田中太郎"},
                {"id": "user-2", "name": "鈴木花子"},
            ],
            "has_more": False,
            "next_cursor": None,
        },
    )

    result = directory.resolve_many(["user-1", "unknown-id", "user-2"])

    assert result == ["田中太郎", "unknown-id", "鈴木花子"]


# --- APIエラー -----------------------------------------------------------------------------------


def test_resolve_raises_notion_user_directory_error_on_401(
    requests_mock, directory: NotionUserDirectory
) -> None:
    requests_mock.get(
        "https://api.notion.com/v1/users",
        status_code=401,
        json={"message": "unauthorized"},
    )

    with pytest.raises(NotionUserDirectoryError):
        directory.resolve("user-1")
