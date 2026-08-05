import pytest

from src.migration.action_mapping import (
    extract_next_action_date_for_project,
    normalize_action_type,
    transform_kintone_action,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("テレアポ", "テレアポ"),
        ("訪問商談", "訪問商談"),
        ("電話", "テレアポ"),
        ("訪問", "訪問商談"),
        ("Web商談", "オンライン商談"),
        (" 自動メール ", "自動メール"),
    ],
)
def test_normalize_action_type_known_values(raw: str, expected: str) -> None:
    assert normalize_action_type(raw) == expected


def test_normalize_action_type_unknown_value_raises() -> None:
    with pytest.raises(ValueError):
        normalize_action_type("FAX送信")


def test_normalize_action_type_none_raises_instead_of_attribute_error() -> None:
    """kintoneの空欄フィールドはNoneで返ってくることがあるため、AttributeErrorにならず
    ValueErrorとして扱われることを確認する。"""
    with pytest.raises(ValueError):
        normalize_action_type(None)


def test_transform_kintone_action_action_type_field_none_raises_value_error() -> None:
    record = {"レコード番号": "4003", "アクション内容": None, "対応者": "営業太郎"}

    with pytest.raises(ValueError):
        transform_kintone_action(record)


def test_transform_kintone_action_maps_expected_fields() -> None:
    record = {
        "レコード番号": "4001",
        "アクション内容": "訪問商談",
        "コメント": "先方担当者と初回打ち合わせ",
        "対応者": "営業太郎",
        "担当者名": "山田太郎",
        "提案サービス": "リピッテ、メイリー",
    }

    result = transform_kintone_action(record)

    assert result == {
        "kintone_Act_ID": "4001",
        "アクション種別": "訪問商談",
        "履歴メモ": "先方担当者と初回打ち合わせ",
        "_担当営業氏名": "営業太郎",
        "_先方担当者氏名": "山田太郎",
        "_提案サービス名リスト": ["リピッテ", "メイリー"],
    }


def test_transform_kintone_action_missing_optional_fields_become_none() -> None:
    record = {"レコード番号": "4002", "アクション内容": "テレアポ", "対応者": "営業太郎"}

    result = transform_kintone_action(record)

    assert result["履歴メモ"] is None
    assert result["_先方担当者氏名"] is None
    assert result["_提案サービス名リスト"] == []


def test_extract_next_action_date_for_project() -> None:
    assert extract_next_action_date_for_project({"次回アクション日": "2026-08-10"}) == "2026-08-10"
    assert extract_next_action_date_for_project({}) is None
