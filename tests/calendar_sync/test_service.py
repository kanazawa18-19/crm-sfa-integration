"""sync_next_action_date_to_calendarの単体テスト。

`calendar_client`にはフェイクのスタブを注入し、`get_notion_user_email`はmonkeypatchでモックする。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.calendar_sync import service as service_module
from src.calendar_sync.service import sync_next_action_date_to_calendar


class _FakeCalendarClient:
    """upsert_event()に渡された引数を記録するフェイクの`calendar_client`。"""

    def __init__(self, return_value: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._return_value = return_value if return_value is not None else {
            "google_event_id": "evt-1",
            "created": True,
        }

    def upsert_event(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._return_value


NOTION_PAGE_ID = "26d6f1e2-0000-0000-0000-000000000000"


def _properties(**overrides: Any) -> dict[str, Any]:
    base = {
        "案件名": "サンプルホテル",
        "次回アクション日": "2026-08-10",
        "担当メンバー": ["user-1"],
    }
    base.update(overrides)
    return base


# --- スキップ条件 ----------------------------------------------------------------------------


def test_returns_none_when_next_action_date_key_missing() -> None:
    properties = _properties()
    del properties["次回アクション日"]
    calendar_client = _FakeCalendarClient()

    result = sync_next_action_date_to_calendar(
        properties, NOTION_PAGE_ID, calendar_client=calendar_client
    )

    assert result is None
    assert calendar_client.calls == []


def test_returns_none_when_next_action_date_is_none() -> None:
    properties = _properties(**{"次回アクション日": None})
    calendar_client = _FakeCalendarClient()

    result = sync_next_action_date_to_calendar(
        properties, NOTION_PAGE_ID, calendar_client=calendar_client
    )

    assert result is None
    assert calendar_client.calls == []


def test_returns_none_when_rep_missing() -> None:
    properties = _properties()
    del properties["担当メンバー"]
    calendar_client = _FakeCalendarClient()

    result = sync_next_action_date_to_calendar(
        properties, NOTION_PAGE_ID, calendar_client=calendar_client
    )

    assert result is None
    assert calendar_client.calls == []


def test_returns_none_when_rep_is_empty_list() -> None:
    properties = _properties(**{"担当メンバー": []})
    calendar_client = _FakeCalendarClient()

    result = sync_next_action_date_to_calendar(
        properties, NOTION_PAGE_ID, calendar_client=calendar_client
    )

    assert result is None
    assert calendar_client.calls == []


def test_returns_none_when_email_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_module, "get_notion_user_email", lambda user_id: None)
    calendar_client = _FakeCalendarClient()

    result = sync_next_action_date_to_calendar(
        _properties(), NOTION_PAGE_ID, calendar_client=calendar_client
    )

    assert result is None
    assert calendar_client.calls == []


# --- 正常系 ----------------------------------------------------------------------------------


def test_calls_upsert_event_with_expected_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_module,
        "get_notion_user_email",
        lambda user_id: "kunikata@cnctor.jp" if user_id == "user-1" else None,
    )
    calendar_client = _FakeCalendarClient()

    result = sync_next_action_date_to_calendar(
        _properties(), NOTION_PAGE_ID, calendar_client=calendar_client
    )

    assert result == {"google_event_id": "evt-1", "created": True}
    assert calendar_client.calls == [
        {
            "rep_email": "kunikata@cnctor.jp",
            "notion_project_id": NOTION_PAGE_ID,
            "summary": "サンプルホテル - 次回アクション",
            "date": "2026-08-10",
        }
    ]


def test_uses_first_rep_when_multiple_reps_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        service_module,
        "get_notion_user_email",
        lambda user_id: {"user-1": "kunikata@cnctor.jp", "user-2": "other@cnctor.jp"}[user_id],
    )
    calendar_client = _FakeCalendarClient()
    properties = _properties(**{"担当メンバー": ["user-1", "user-2"]})

    sync_next_action_date_to_calendar(properties, NOTION_PAGE_ID, calendar_client=calendar_client)

    assert calendar_client.calls[0]["rep_email"] == "kunikata@cnctor.jp"


def test_strips_time_component_from_next_action_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """回帰確認: Notion側で「時間を含める」がONのページはISO日時文字列
    （例: "2026-08-10T09:00:00+09:00"）になる。web-engagement-tool側のAPIは
    YYYY-MM-DD形式のみを受け付けるため、日付部分のみを渡すこと。
    """
    monkeypatch.setattr(service_module, "get_notion_user_email", lambda user_id: "kunikata@cnctor.jp")
    calendar_client = _FakeCalendarClient()
    properties = _properties(**{"次回アクション日": "2026-08-10T09:00:00+09:00"})

    sync_next_action_date_to_calendar(properties, NOTION_PAGE_ID, calendar_client=calendar_client)

    assert calendar_client.calls[0]["date"] == "2026-08-10"


def test_uses_fallback_summary_when_project_name_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(service_module, "get_notion_user_email", lambda user_id: "kunikata@cnctor.jp")
    calendar_client = _FakeCalendarClient()
    properties = _properties()
    del properties["案件名"]

    sync_next_action_date_to_calendar(properties, NOTION_PAGE_ID, calendar_client=calendar_client)

    assert calendar_client.calls[0]["summary"] == "（案件名未設定） - 次回アクション"
