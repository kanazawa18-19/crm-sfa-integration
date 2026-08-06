from __future__ import annotations

import pytest

from src.api.action_classifier import classify_action_type


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("【電話】3回目", "テレアポ"),
        ("テレアポ↓（田中）", "テレアポ"),
        ("TELした", "テレアポ"),
        ("【商談】2回目（訪問）", "訪問商談"),
        ("訪問商談実施", "訪問商談"),
        ("WEB商談 2回目", "オンライン商談"),
        ("オンライン商談", "オンライン商談"),
        ("Zoomで打ち合わせ", "オンライン商談"),
        ("zoomで打ち合わせ", "オンライン商談"),
        ("メール送付", "メール"),
        ("Mail送付", "メール"),
        ("その他の対応", "その他"),
        (None, "その他"),
        ("", "その他"),
    ],
)
def test_classify_action_type(title: str | None, expected: str) -> None:
    assert classify_action_type(title) == expected


def test_classify_action_type_priority_tel_over_visit() -> None:
    """優先順位1（テレアポ/電話/TEL）が優先順位2（訪問）より先に判定されることを確認する。"""
    assert classify_action_type("電話→訪問の予定") == "テレアポ"
