"""06_営業分析ロジック「総接触回数の自動カウント」「チャネル別内訳」の検証。"""

from __future__ import annotations

from datetime import date

from src.analytics.contact_count import ActionRecord, count_by_channel, count_total_contacts


def test_count_total_contacts_sums_countable_action_types_per_project() -> None:
    actions = [
        ActionRecord(project_id="MSA-PJ-001", action_type="自動メール", action_date=date(2026, 8, 1)),
        ActionRecord(project_id="MSA-PJ-001", action_type="テレアポ", action_date=date(2026, 8, 2)),
        ActionRecord(project_id="MSA-PJ-001", action_type="訪問商談", action_date=date(2026, 8, 3)),
        ActionRecord(project_id="MSA-PJ-002", action_type="オンライン商談", action_date=date(2026, 8, 1)),
    ]

    result = count_total_contacts(actions)

    assert result == {"MSA-PJ-001": 3, "MSA-PJ-002": 1}


def test_count_total_contacts_excludes_manual_email() -> None:
    """人力の「メール」は仕様書の集計対象一覧に無いため除外する。"""
    actions = [
        ActionRecord(project_id="MSA-PJ-001", action_type="メール"),
        ActionRecord(project_id="MSA-PJ-001", action_type="テレアポ"),
    ]

    result = count_total_contacts(actions)

    assert result == {"MSA-PJ-001": 1}


def test_count_total_contacts_empty_input_returns_empty_dict() -> None:
    assert count_total_contacts([]) == {}


def test_count_total_contacts_project_with_only_uncountable_actions_is_absent() -> None:
    actions = [ActionRecord(project_id="MSA-PJ-003", action_type="メール")]

    result = count_total_contacts(actions)

    assert result == {}
    assert result.get("MSA-PJ-003", 0) == 0


def test_count_by_channel_breaks_down_by_action_type_per_project() -> None:
    actions = [
        ActionRecord(project_id="MSA-PJ-001", action_type="自動メール"),
        ActionRecord(project_id="MSA-PJ-001", action_type="自動メール"),
        ActionRecord(project_id="MSA-PJ-001", action_type="自動メール"),
        ActionRecord(project_id="MSA-PJ-001", action_type="テレアポ"),
        ActionRecord(project_id="MSA-PJ-001", action_type="テレアポ"),
        ActionRecord(project_id="MSA-PJ-001", action_type="オンライン商談"),
        ActionRecord(project_id="MSA-PJ-001", action_type="オンライン商談"),
        ActionRecord(project_id="MSA-PJ-001", action_type="訪問商談"),
        ActionRecord(project_id="MSA-PJ-001", action_type="メール"),
    ]

    result = count_by_channel(actions)

    assert result == {
        "MSA-PJ-001": {
            "自動メール": 3,
            "テレアポ": 2,
            "オンライン商談": 2,
            "訪問商談": 1,
        }
    }
