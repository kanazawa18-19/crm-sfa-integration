"""get_notion_user_emailの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

from src.calendar_sync.notion_user_email import get_notion_user_email

USER_ID = "0fa87cdd-c868-4483-a394-8736b7e65d62"


def test_get_notion_user_email_returns_email_on_success(requests_mock) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/users/{USER_ID}",
        json={
            "object": "user",
            "id": USER_ID,
            "name": "國方勇樹",
            "type": "person",
            "person": {"email": "kunikata@cnctor.jp", "email_verified": True},
        },
    )

    assert get_notion_user_email(USER_ID, api_key="secret-notion-key") == "kunikata@cnctor.jp"


def test_get_notion_user_email_returns_none_on_404(requests_mock) -> None:
    requests_mock.get(f"https://api.notion.com/v1/users/{USER_ID}", status_code=404)

    assert get_notion_user_email(USER_ID, api_key="secret-notion-key") is None


def test_get_notion_user_email_returns_none_when_person_key_missing(requests_mock) -> None:
    """botユーザー等、`person`キーが無いレスポンス。"""
    requests_mock.get(
        f"https://api.notion.com/v1/users/{USER_ID}",
        json={"object": "user", "id": USER_ID, "name": "Integration Bot", "type": "bot"},
    )

    assert get_notion_user_email(USER_ID, api_key="secret-notion-key") is None


def test_get_notion_user_email_returns_none_when_email_missing(requests_mock) -> None:
    requests_mock.get(
        f"https://api.notion.com/v1/users/{USER_ID}",
        json={
            "object": "user",
            "id": USER_ID,
            "name": "國方勇樹",
            "type": "person",
            "person": {},
        },
    )

    assert get_notion_user_email(USER_ID, api_key="secret-notion-key") is None
