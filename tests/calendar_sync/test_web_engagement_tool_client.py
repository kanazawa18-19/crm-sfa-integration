"""WebEngagementToolCalendarClientの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

import pytest

from src.calendar_sync.web_engagement_tool_client import (
    CalendarSyncApiError,
    WebEngagementToolCalendarClient,
)


@pytest.fixture
def client() -> WebEngagementToolCalendarClient:
    return WebEngagementToolCalendarClient(
        base_url="http://localhost:3001", api_token="secret-calendar-token"
    )


# --- コンストラクタのバリデーション ---------------------------------------------------------


def test_init_raises_value_error_when_base_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_ENGAGEMENT_TOOL_URL", raising=False)
    with pytest.raises(ValueError):
        WebEngagementToolCalendarClient(base_url=None, api_token="secret-calendar-token")


def test_init_raises_value_error_when_api_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CALENDAR_SYNC_API_TOKEN", raising=False)
    with pytest.raises(ValueError):
        WebEngagementToolCalendarClient(base_url="http://localhost:3001", api_token=None)


# --- upsert_event: 正常系 -------------------------------------------------------------------


def test_upsert_event_returns_created_event_on_200(
    requests_mock, client: WebEngagementToolCalendarClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/calendar/events",
        json={"google_event_id": "evt-1", "created": True},
    )

    result = client.upsert_event(
        rep_email="kunikata@cnctor.jp",
        notion_project_id="26d6f1e2-0000-0000-0000-000000000000",
        summary="サンプルホテル - 次回アクション",
        date="2026-08-10",
    )

    assert result == {"google_event_id": "evt-1", "created": True}
    sent_body = requests_mock.request_history[0].json()
    assert sent_body == {
        "rep_email": "kunikata@cnctor.jp",
        "notion_project_id": "26d6f1e2-0000-0000-0000-000000000000",
        "summary": "サンプルホテル - 次回アクション",
        "date": "2026-08-10",
    }
    assert requests_mock.request_history[0].headers["Authorization"] == "Bearer secret-calendar-token"


def test_upsert_event_returns_updated_event_when_created_false(
    requests_mock, client: WebEngagementToolCalendarClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/calendar/events",
        json={"google_event_id": "evt-1", "created": False},
    )

    result = client.upsert_event(
        rep_email="kunikata@cnctor.jp",
        notion_project_id="26d6f1e2-0000-0000-0000-000000000000",
        summary="サンプルホテル - 次回アクション",
        date="2026-08-10",
    )

    assert result == {"google_event_id": "evt-1", "created": False}


# --- upsert_event: rep_not_connected（422） -------------------------------------------------


def test_upsert_event_returns_skip_dict_for_rep_not_connected(
    requests_mock, client: WebEngagementToolCalendarClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/calendar/events",
        status_code=422,
        json={"error": "rep_not_connected", "rep_email": "kunikata@cnctor.jp"},
    )

    result = client.upsert_event(
        rep_email="kunikata@cnctor.jp",
        notion_project_id="26d6f1e2-0000-0000-0000-000000000000",
        summary="サンプルホテル - 次回アクション",
        date="2026-08-10",
    )

    assert result == {"skipped": "rep_not_connected", "rep_email": "kunikata@cnctor.jp"}


# --- upsert_event: その他のエラー ------------------------------------------------------------


def test_upsert_event_raises_calendar_sync_api_error_on_401(
    requests_mock, client: WebEngagementToolCalendarClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/calendar/events",
        status_code=401,
        json={"message": "unauthorized"},
    )

    with pytest.raises(CalendarSyncApiError):
        client.upsert_event(
            rep_email="kunikata@cnctor.jp",
            notion_project_id="26d6f1e2-0000-0000-0000-000000000000",
            summary="サンプルホテル - 次回アクション",
            date="2026-08-10",
        )


def test_upsert_event_raises_calendar_sync_api_error_on_400(
    requests_mock, client: WebEngagementToolCalendarClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/calendar/events",
        status_code=400,
        json={"message": "invalid request"},
    )

    with pytest.raises(CalendarSyncApiError):
        client.upsert_event(
            rep_email="kunikata@cnctor.jp",
            notion_project_id="26d6f1e2-0000-0000-0000-000000000000",
            summary="サンプルホテル - 次回アクション",
            date="2026-08-10",
        )


def test_upsert_event_raises_calendar_sync_api_error_on_500(
    requests_mock, client: WebEngagementToolCalendarClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(
        "http://localhost:3001/api/calendar/events",
        status_code=500,
        json={"message": "internal error"},
    )

    with pytest.raises(CalendarSyncApiError):
        client.upsert_event(
            rep_email="kunikata@cnctor.jp",
            notion_project_id="26d6f1e2-0000-0000-0000-000000000000",
            summary="サンプルホテル - 次回アクション",
            date="2026-08-10",
        )
