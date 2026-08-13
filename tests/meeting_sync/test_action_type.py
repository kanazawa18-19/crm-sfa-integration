from __future__ import annotations

import pytest

from src.meeting_sync.action_type import infer_action_type


@pytest.mark.parametrize(
    "title,has_meet_link,expected",
    [
        ("【商談（訪問）】〇〇ホテル様", False, "訪問商談"),
        ("【商談（訪問）】〇〇ホテル様", True, "訪問商談"),
        ("【商談（WEB）】〇〇ホテル様", False, "オンライン商談"),
        ("【商談（Web）】〇〇ホテル様", False, "オンライン商談"),
        ("【商談（オンライン）】〇〇ホテル様", False, "オンライン商談"),
    ],
)
def test_infer_action_type_from_bracket_pattern(
    title: str, has_meet_link: bool, expected: str
) -> None:
    assert infer_action_type(title, has_meet_link=has_meet_link) == expected


def test_infer_action_type_falls_back_to_meet_link_when_no_bracket_pattern() -> None:
    assert infer_action_type("〇〇ホテル様 打ち合わせ", has_meet_link=True) == "オンライン商談"
    assert infer_action_type("〇〇ホテル様 打ち合わせ", has_meet_link=False) == "訪問商談"


def test_infer_action_type_never_returns_empty() -> None:
    # アクション種別はNotion側でRequirementLevel.REQUIREDのため、常に何か値を返す必要がある。
    assert infer_action_type("", has_meet_link=False)
    assert infer_action_type(None, has_meet_link=False)  # type: ignore[arg-type]
