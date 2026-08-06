import pytest

from src.db_schema.project import (
    ACTIVE_STATUSES,
    CANCELLED_STATUSES,
    CONFIDENCE_LEVELS,
    CONFIRMED_STATUSES,
    LOST_STATUSES,
    PROJECT_SCHEMA,
    classify_status,
)


def test_status_partition_covers_all_real_data_values_without_overlap() -> None:
    """実データの営業ステータス11値が4区分のいずれか1つに過不足なく属することを検証する。"""
    all_real_values = {
        "施設契約",
        "解約",
        "リスケ",
        "失注",
        "アポ",
        "Dヨミ",
        "Cヨミ",
        "Bヨミ",
        "Aヨミ",
        "トライアル",
        "契約",
    }
    groups = [CONFIRMED_STATUSES, CANCELLED_STATUSES, LOST_STATUSES, ACTIVE_STATUSES]
    union = set().union(*groups)
    assert union == all_real_values

    # 重複が無い（1値が複数区分に属さない）ことも検証する
    total_len = sum(len(g) for g in groups)
    assert total_len == len(union)


@pytest.mark.parametrize(
    "status,expected",
    [
        ("施設契約", "契約済"),
        ("契約", "契約済"),
        ("解約", "解約"),
        ("失注", "失注"),
        ("リスケ", "進行中"),
        ("アポ", "進行中"),
        ("Dヨミ", "進行中"),
        ("Cヨミ", "進行中"),
        ("Bヨミ", "進行中"),
        ("Aヨミ", "進行中"),
        ("トライアル", "進行中"),
    ],
)
def test_classify_status_maps_to_expected_category(status: str, expected: str) -> None:
    assert classify_status(status) == expected


def test_classify_status_unknown_value_raises_value_error() -> None:
    with pytest.raises(ValueError):
        classify_status("未知のステータス")


def test_confidence_levels_order_is_a_to_d() -> None:
    assert CONFIDENCE_LEVELS == ("A", "B", "C", "D")


def test_project_schema_status_property_options_match_real_data() -> None:
    status_prop = PROJECT_SCHEMA.get_property("営業ステータス")
    assert set(status_prop.options) == {
        "施設契約",
        "解約",
        "リスケ",
        "失注",
        "アポ",
        "Dヨミ",
        "Cヨミ",
        "Bヨミ",
        "Aヨミ",
        "トライアル",
        "契約",
    }


def test_project_schema_confidence_property_options_are_a_to_d() -> None:
    confidence_prop = PROJECT_SCHEMA.get_property("確度")
    assert confidence_prop.options == CONFIDENCE_LEVELS


def test_project_schema_notion_database_id_matches_real_data() -> None:
    assert PROJECT_SCHEMA.notion_database_id == "418adcbb-3714-4c90-9c04-da0bfca4df09"
