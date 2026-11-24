"""通信なしで優先順位、空欄、日付とプレビュー内容を検証する。"""

import pytest

from src.calendar_sync.plan import build_event_plan, calendar_properties

PAGE = "26d6f1e2-0000-0000-0000-000000000000"


def test_manual_action_wins_and_full_text_is_kept():
    action = "資料を送る\n" + "詳しい説明" * 40
    plan = build_event_plan({"次回アクション": action, "AI推奨アクション": "電話する",
                             "AI推奨アクション日": "2026-11-25", "案件名": "検証案件"}, PAGE)
    assert plan["summary"].startswith("検証案件 - 資料を送る ")
    assert plan["summary"].endswith("…")
    assert action in plan["description"]
    assert "電話する" not in plan["description"]
    assert PAGE.replace("-", "") in plan["description"]


def test_blank_manual_action_uses_ai():
    plan = build_event_plan({"次回アクション": " \n", "AI推奨アクション": "電話する",
                             "AI推奨アクション日": "2026-11-25T09:00:00+09:00"}, PAGE)
    assert plan["date"] == "2026-11-25"
    assert plan["description"].startswith("AI推奨アクション\n電話する")


@pytest.mark.parametrize("properties", [{}, {"次回アクション": "電話する"},
                                      {"AI推奨アクション日": "2026-11-25"}])
def test_missing_content_or_date_is_not_scheduled(properties):
    assert build_event_plan(properties, PAGE) is None


@pytest.mark.parametrize("value", ["2026-02-30", "明日", "2026-11-25Tbad", 123])
def test_invalid_date_is_rejected(value):
    with pytest.raises(ValueError):
        build_event_plan({"次回アクション": "電話する", "AI推奨アクション日": value}, PAGE)


def test_raw_properties_support_text_and_formula_without_other_fields():
    result = calendar_properties({
        "AI推奨アクション": {"type": "formula", "formula": {"type": "string", "string": "電話する"}},
        "次回アクション": {"type": "rich_text", "rich_text": [{"text": {"content": "資料"}}, {"plain_text": "送付"}]},
        "担当メンバー": {"type": "people", "people": [{"id": "user-1"}]},
        "AI推奨アクション日": {"type": "date", "date": {"start": "2026-11-25"}},
        "電話番号": {"type": "phone_number", "phone_number": "対象外"},
    })
    assert result == {"AI推奨アクション": "電話する", "次回アクション": "資料送付",
                      "担当メンバー": ["user-1"], "AI推奨アクション日": ["2026-11-25"]}


def test_preview_is_offline_and_explains_missing_date(monkeypatch):
    from src.calendar_sync.preview import preview_page
    from src.calendar_sync import service
    monkeypatch.setattr(service, "get_notion_user_email", lambda *_: pytest.fail("外部参照禁止"))
    page = {"id": PAGE, "properties": {
        "次回アクション": {"type": "rich_text", "rich_text": [{"plain_text": "電話する"}]},
    }}
    assert preview_page(page) == {"status": "保留", "reason": "アクション内容・AI推奨アクション日の未入力、またはアクションなしの指定"}
    page["properties"].update({
        "AI推奨アクション日": {"type": "date", "date": {"start": "2026-11-25"}},
        "担当メンバー": {"type": "people", "people": [{"id": "user-1"}]},
    })
    preview = preview_page(page)
    assert preview["status"] == "登録候補"
    assert preview["notion_user_id"] == "user-1"
    assert preview["event"]["date"] == "2026-11-25"
    page["in_trash"] = True
    assert preview_page(page)["status"] == "保留"


@pytest.mark.parametrize("value, expected", [
    ("2026-11-30 / 2026-11-25 / 2026-11-20", "2026-11-25"),
    ("2026年11月30日、2026年11月24日", "2026-11-24"),
    (["2026-11-01", "2026-11-22"], "2026-11-22"),
    (["2026-11-30", "2026-11-25"], "2026-11-25"),
])
def test_select_nearest_explicit_day(value, expected):
    from datetime import date
    from src.calendar_sync.plan import select_action_date
    assert select_action_date(value, today=date(2026, 11, 24)).isoformat() == expected


def test_old_date_is_not_used_as_fallback():
    assert build_event_plan({"次回アクション": "電話する", "次回アクション日": "2026-11-25"}, PAGE) is None


@pytest.mark.parametrize("action", ["なし", "アクションなし", "対応不要"])
def test_explicit_no_action_does_not_use_ai_fallback(action):
    assert build_event_plan({"次回アクション": action, "AI推奨アクション": "電話する",
                             "AI推奨アクション日": "2026-11-25"}, PAGE) is None


@pytest.mark.parametrize("value", [" 2026-11-25Tbad", "2026-11-25garbage", "2026年11/25",
                                   "2026-11-25Tbad、2026-12-01"])
def test_invalid_candidate_is_not_partially_accepted(value):
    from src.calendar_sync.plan import select_action_date
    with pytest.raises(ValueError):
        select_action_date(value)
