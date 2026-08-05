"""07_日報週報仕様「チーム日報」の生成ロジックの検証。"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from src.reports.daily_report import (
    DailyActionRecord,
    DailyProjectRecord,
    build_daily_report_data,
    generate_daily_report_text,
    next_business_day,
)

REPORT_DATE = date(2026, 8, 5)  # 2026-08-05は水曜日


# --- next_business_day ---


def test_next_business_day_skips_weekend_from_friday() -> None:
    assert next_business_day(date(2026, 8, 7)) == date(2026, 8, 10)  # 金曜 -> 月曜


def test_next_business_day_is_just_the_following_day_on_weekday() -> None:
    assert next_business_day(REPORT_DATE) == date(2026, 8, 6)  # 水曜 -> 木曜


def test_next_business_day_skips_weekend_from_saturday() -> None:
    assert next_business_day(date(2026, 8, 8)) == date(2026, 8, 10)  # 土曜 -> 月曜


# --- build_daily_report_data: メンバー別アクション件数サマリー ---


def test_member_action_summary_breaks_down_by_type_and_excludes_automated_mail() -> None:
    actions = [
        DailyActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=REPORT_DATE),
        DailyActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=REPORT_DATE),
        DailyActionRecord(project_id="P2", member="佐藤", action_type="訪問商談", action_date=REPORT_DATE),
        DailyActionRecord(project_id="P3", member="鈴木", action_type="メール", action_date=REPORT_DATE),
        # 自動メールは日報のメンバー別集計対象外（人が行った活動ではないため）
        DailyActionRecord(project_id="P4", member="鈴木", action_type="自動メール", action_date=REPORT_DATE),
    ]

    data = build_daily_report_data(report_date=REPORT_DATE, actions=actions, projects=[])

    assert len(data.member_summaries) == 2
    sato = next(s for s in data.member_summaries if s.member == "佐藤")
    suzuki = next(s for s in data.member_summaries if s.member == "鈴木")
    assert sato.counts_by_type == {"テレアポ": 2, "訪問商談": 1}
    assert sato.total == 3
    assert suzuki.counts_by_type == {"メール": 1}
    assert suzuki.total == 1


def test_member_action_summary_is_empty_when_no_actions() -> None:
    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=[])

    assert data.member_summaries == ()


def test_member_action_summary_filters_to_report_date_only() -> None:
    actions = [
        DailyActionRecord(
            project_id="P1", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 3)
        ),  # 前々日 -> 対象外
        DailyActionRecord(
            project_id="P1", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 4)
        ),  # 前日 -> 対象外
        DailyActionRecord(
            project_id="P1", member="佐藤", action_type="訪問商談", action_date=REPORT_DATE
        ),  # 当日 -> 対象
    ]

    data = build_daily_report_data(report_date=REPORT_DATE, actions=actions, projects=[])

    assert len(data.member_summaries) == 1
    sato = data.member_summaries[0]
    assert sato.counts_by_type == {"訪問商談": 1}
    assert sato.total == 1


def test_member_action_summary_warns_on_unknown_action_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    actions = [
        DailyActionRecord(
            project_id="P1", member="佐藤", action_type="謎の種別", action_date=REPORT_DATE
        ),
    ]

    with caplog.at_level(logging.WARNING):
        data = build_daily_report_data(report_date=REPORT_DATE, actions=actions, projects=[])

    assert data.member_summaries == ()
    assert any("謎の種別" in record.message for record in caplog.records)


# --- build_daily_report_data: 新規獲得案件リスト ---


def test_new_projects_filters_to_report_date_only() -> None:
    projects = [
        DailyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="初回接触",
            created_date=REPORT_DATE,
            proposed_services=("サービスX",),
            initial_fee=100000,
            monthly_fee=10000,
        ),
        DailyProjectRecord(
            project_id="P2",
            client_name="株式会社B",
            assignee="鈴木",
            status="初回接触",
            created_date=date(2026, 8, 4),  # 前日作成 -> 対象外
        ),
    ]

    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=projects)

    assert len(data.new_projects) == 1
    assert data.new_projects[0].client_name == "株式会社A"
    assert data.new_projects[0].proposed_services == ("サービスX",)


def test_new_projects_is_empty_when_none_created_today() -> None:
    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=[])

    assert data.new_projects == ()


# --- build_daily_report_data: ステータス変更のあった案件一覧 ---


def test_status_changes_filters_to_report_date_and_requires_previous_status() -> None:
    projects = [
        DailyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="提案中",
            created_date=date(2026, 7, 1),
            confidence="A",
            previous_status="初回接触",
            status_changed_date=REPORT_DATE,
        ),
        DailyProjectRecord(
            project_id="P2",
            client_name="株式会社B",
            assignee="鈴木",
            status="提案中",
            created_date=date(2026, 7, 1),
            status_changed_date=date(2026, 8, 4),  # 前日変更 -> 対象外
            previous_status="初回接触",
        ),
        DailyProjectRecord(
            project_id="P3",
            client_name="株式会社C",
            assignee="鈴木",
            status="初回接触",
            created_date=date(2026, 7, 1),
        ),  # ステータス変更なし -> 対象外
    ]

    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=projects)

    assert len(data.status_changes) == 1
    change = data.status_changes[0]
    assert change.client_name == "株式会社A"
    assert change.previous_status == "初回接触"
    assert change.new_status == "提案中"
    assert change.confidence == "A"


def test_status_changes_is_empty_when_none_changed_today() -> None:
    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=[])

    assert data.status_changes == ()


def test_status_changes_excludes_project_changed_today_without_previous_status() -> None:
    """status_changed_date == report_dateでも、previous_statusがNoneなら対象外とする。

    案件管理DBのスナップショット自体には変更履歴が無く、previous_statusは
    呼び出し側（変更検知ロジック）が判定できなかった場合にNoneとなりうる想定
    （モジュールdocstring参照）。この場合、変更前ステータスを表示できないため
    一覧から除外する。
    """
    projects = [
        DailyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="提案中",
            created_date=date(2026, 7, 1),
            status_changed_date=REPORT_DATE,
            previous_status=None,
        ),
    ]

    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=projects)

    assert data.status_changes == ()


# --- build_daily_report_data: 翌営業日の次回アクション予定一覧 ---


def test_upcoming_actions_filters_to_next_business_day() -> None:
    projects = [
        DailyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="提案中",
            created_date=date(2026, 7, 1),
            next_action_date=date(2026, 8, 6),  # 翌営業日（木）
        ),
        DailyProjectRecord(
            project_id="P2",
            client_name="株式会社B",
            assignee="鈴木",
            status="提案中",
            created_date=date(2026, 7, 1),
            next_action_date=date(2026, 8, 10),  # 翌々営業日以降 -> 対象外
        ),
    ]

    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=projects)

    assert len(data.upcoming_actions) == 1
    assert data.upcoming_actions[0].client_name == "株式会社A"


def test_upcoming_actions_uses_next_business_day_across_weekend() -> None:
    friday = date(2026, 8, 7)
    projects = [
        DailyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="提案中",
            created_date=date(2026, 7, 1),
            next_action_date=date(2026, 8, 10),  # 金曜の翌営業日は月曜
        ),
    ]

    data = build_daily_report_data(report_date=friday, actions=[], projects=projects)

    assert data.next_business_day == date(2026, 8, 10)
    assert len(data.upcoming_actions) == 1


def test_upcoming_actions_is_empty_when_none_scheduled() -> None:
    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=[])

    assert data.upcoming_actions == ()


# --- generate_daily_report_text ---


def test_generate_daily_report_text_renders_all_sections() -> None:
    actions = [
        DailyActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=REPORT_DATE),
    ]
    projects = [
        DailyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="初回接触",
            created_date=REPORT_DATE,
            proposed_services=("サービスX",),
            initial_fee=100000,
            monthly_fee=10000,
        ),
        DailyProjectRecord(
            project_id="P2",
            client_name="株式会社B",
            assignee="鈴木",
            status="提案中",
            created_date=date(2026, 7, 1),
            confidence="A",
            previous_status="初回接触",
            status_changed_date=REPORT_DATE,
        ),
        DailyProjectRecord(
            project_id="P3",
            client_name="株式会社C",
            assignee="佐藤",
            status="提案中",
            created_date=date(2026, 7, 1),
            next_action_date=date(2026, 8, 6),
        ),
    ]

    data = build_daily_report_data(report_date=REPORT_DATE, actions=actions, projects=projects)
    text = generate_daily_report_text(data)

    assert "2026-08-05" in text
    assert "佐藤: 計1件" in text
    assert "株式会社A" in text
    assert "サービスX" in text
    assert "株式会社B" in text
    assert "初回接触 → 提案中" in text
    assert "株式会社C" in text
    assert "2026-08-06" in text


def test_generate_daily_report_text_renders_placeholder_when_all_sections_are_empty() -> None:
    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=[])

    text = generate_daily_report_text(data)

    assert "本日のアクション実績はありません" in text
    assert "本日の新規獲得案件はありません" in text
    assert "本日ステータスが変更された案件はありません" in text
    assert "翌営業日に予定されている次回アクションはありません" in text


def test_generate_daily_report_text_raises_readable_error_on_broken_template(
    tmp_path: Path,
) -> None:
    """テンプレートのプレースホルダが不正な場合、無言のKeyErrorではなく
    分かりやすいエラーメッセージのValueErrorになること（非エンジニアの運用担当者が
    テンプレートを編集する前提のため）。
    """
    broken_template = tmp_path / "broken_daily_report.txt"
    broken_template.write_text("日報 {report_date} {存在しないプレースホルダ}", encoding="utf-8")
    data = build_daily_report_data(report_date=REPORT_DATE, actions=[], projects=[])

    with pytest.raises(ValueError, match="存在しないプレースホルダ"):
        generate_daily_report_text(data, template_path=broken_template)
