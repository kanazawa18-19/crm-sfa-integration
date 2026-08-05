"""07_日報週報仕様「チーム週報」の生成ロジックの検証。"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from src.analytics.member_performance import MemberActionRecord
from src.analytics.win_pattern import ProposalRecord
from src.analytics.win_rate import ProjectOutcome
from src.reports.weekly_report import (
    RevenueTarget,
    WeeklyProjectRecord,
    build_weekly_report_data,
    generate_weekly_report_text,
)

WEEK_START = date(2026, 8, 3)  # 月
WEEK_END = date(2026, 8, 7)  # 金
MONTH_START = date(2026, 8, 1)
MONTH_END = date(2026, 8, 31)
QUARTER_START = date(2026, 7, 1)
QUARTER_END = date(2026, 9, 30)

NO_TARGET = RevenueTarget(initial_fee=0.0, mrr=0.0)


def _build(
    *,
    active_projects=(),
    historical_outcomes=(),
    proposal_records=(),
    member_actions=(),
    monthly_target=NO_TARGET,
    quarter_target=NO_TARGET,
    as_of=None,
):
    return build_weekly_report_data(
        week_start=WEEK_START,
        week_end=WEEK_END,
        month_start=MONTH_START,
        month_end=MONTH_END,
        quarter_start=QUARTER_START,
        quarter_end=QUARTER_END,
        active_projects=list(active_projects),
        historical_outcomes=list(historical_outcomes),
        proposal_records=list(proposal_records),
        member_actions=list(member_actions),
        monthly_target=monthly_target,
        quarter_target=quarter_target,
        as_of=as_of,
    )


# --- 今週の確定売上／獲得MRR ---


def test_weekly_confirmed_revenue_sums_only_contracts_within_the_week() -> None:
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            initial_fee=100000,
            monthly_fee=10000,
            contract_date=date(2026, 8, 5),  # 週内
        ),
        WeeklyProjectRecord(
            project_id="P2",
            client_name="株式会社B",
            assignee="佐藤",
            status="契約済",
            initial_fee=999999,
            monthly_fee=99999,
            contract_date=date(2026, 8, 1),  # 週外（先週）
        ),
        WeeklyProjectRecord(
            project_id="P3",
            client_name="株式会社C",
            assignee="佐藤",
            status="提案中",  # 未契約 -> 対象外
            initial_fee=999999,
            monthly_fee=99999,
            contract_date=date(2026, 8, 5),
        ),
    ]

    data = _build(active_projects=projects)

    assert data.weekly_confirmed_initial_fee == 100000
    assert data.weekly_confirmed_mrr == 10000


def test_weekly_confirmed_revenue_is_zero_when_no_projects() -> None:
    data = _build()

    assert data.weekly_confirmed_initial_fee == 0
    assert data.weekly_confirmed_mrr == 0


# --- active_projectsにクオーター範囲外の契約済みレコードが混入した場合の防御的チェック ---


def test_warns_when_confirmed_project_has_contract_date_outside_quarter(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """active_projectsに案件管理DB全件（過去クオーターの契約済み案件を含む）が
    誤って渡された場合に備え、クオーター範囲外の契約日を検知したら警告する。
    """
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            initial_fee=100000,
            monthly_fee=10000,
            contract_date=date(2026, 4, 1),  # 前クオーター -> 範囲外
        ),
    ]

    with caplog.at_level(logging.WARNING):
        data = _build(active_projects=projects)

    assert any("P1" in record.message for record in caplog.records)
    # 警告は出すが除外はしない（週内・月内には含まれないが、docstring通りの挙動）
    assert data.weekly_confirmed_initial_fee == 0


def test_does_not_warn_when_confirmed_project_contract_date_is_within_quarter() -> None:
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            initial_fee=100000,
            monthly_fee=10000,
            contract_date=date(2026, 8, 5),  # 当クオーター内
        ),
    ]

    data = _build(active_projects=projects)

    assert data.weekly_confirmed_initial_fee == 100000


# --- 月次・クオーター目標に対する進捗率 ---


def test_progress_rate_is_none_when_target_is_zero() -> None:
    data = _build()

    assert data.monthly_progress.initial_fee_progress_rate is None
    assert data.monthly_progress.mrr_progress_rate is None


def test_progress_rate_is_computed_against_target() -> None:
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            initial_fee=500000,
            monthly_fee=50000,
            contract_date=date(2026, 8, 5),
        ),
    ]

    data = _build(
        active_projects=projects,
        monthly_target=RevenueTarget(initial_fee=1000000, mrr=100000),
    )

    assert data.monthly_progress.initial_fee_progress_rate == 50.0
    assert data.monthly_progress.mrr_progress_rate == 50.0


# --- 営業パフォーマンス分析 ---


def test_average_won_contact_count_is_none_when_no_historical_wins() -> None:
    data = _build()

    assert data.average_won_contact_count is None


def test_average_won_contact_count_uses_historical_outcomes() -> None:
    outcomes = [
        ProjectOutcome(project_id="H1", total_contact_count=4, is_won=True),
        ProjectOutcome(project_id="H2", total_contact_count=6, is_won=True),
        ProjectOutcome(project_id="H3", total_contact_count=100, is_won=False),
    ]

    data = _build(historical_outcomes=outcomes)

    assert data.average_won_contact_count == 5.0


def test_win_patterns_excludes_low_sample_size_combinations() -> None:
    proposals = [
        ProposalRecord(project_id=f"P{i}", meeting_number=1, services=frozenset({"サービスX"}), is_won=True)
        for i in range(3)
    ] + [ProposalRecord(project_id="P4", meeting_number=2, services=frozenset({"サービスY"}), is_won=True)]

    data = _build(proposal_records=proposals)

    assert len(data.win_patterns) == 1
    assert data.win_patterns[0].meeting_number == 1


def test_win_patterns_is_empty_when_no_proposal_records() -> None:
    data = _build()

    assert data.win_patterns == ()


# --- メンバー別パフォーマンス ---


def test_member_performances_is_empty_when_no_projects_or_actions() -> None:
    data = _build(as_of=WEEK_END)

    assert data.member_performances == ()


def test_member_performances_overall_score_is_none_when_quality_undetermined() -> None:
    """担当案件が全てアクティブ（決着済み0件）のメンバーは総合スコアが未確定。"""
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="提案中",
        ),
    ]

    data = _build(active_projects=projects, as_of=WEEK_END)

    assert len(data.member_performances) == 1
    perf = data.member_performances[0]
    assert perf.member == "佐藤"
    assert perf.quality_win_rate is None
    assert perf.overall_score is None


def test_member_performances_reflects_win_rate_and_deadline_compliance() -> None:
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            contract_date=date(2026, 8, 5),
        ),
        WeeklyProjectRecord(
            project_id="P2",
            client_name="株式会社B",
            assignee="佐藤",
            status="失注",
        ),
        WeeklyProjectRecord(
            project_id="P3",
            client_name="株式会社C",
            assignee="佐藤",
            status="商談中(B)",
            next_action_date=date(2026, 8, 1),  # WEEK_END(8/7)より過去 -> 期限判定対象
        ),
    ]
    actions = [
        MemberActionRecord(project_id="P3", member="佐藤", action_type="テレアポ", action_date=date(2026, 8, 2)),
    ]

    data = _build(active_projects=projects, member_actions=actions, as_of=WEEK_END)

    assert len(data.member_performances) == 1
    perf = data.member_performances[0]
    assert perf.member == "佐藤"
    assert perf.quality_win_rate == 1 / 2  # 契約済1件 / 決着済み(契約済+失注)2件
    assert perf.speed_compliance_rate == 1.0  # P3の期限超過後にフォロー実施済み
    assert perf.volume_score == 1.0  # グループ内唯一のメンバーなので常に1.0
    assert perf.overall_score == 0.5  # volume_score(1.0) * quality(0.5) * speed(1.0)


def test_member_performances_is_absent_from_data_when_no_member_actions_passed() -> None:
    """member_actionsを省略しても他セクションは通常通り動作すること。"""
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            contract_date=date(2026, 8, 5),
        ),
    ]

    data = _build(active_projects=projects, as_of=WEEK_END)

    assert len(data.member_performances) == 1
    assert data.member_performances[0].volume_contact_count == 0


# --- クオーター着地予測 ---


def test_quarter_forecast_reflects_active_projects() -> None:
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            initial_fee=100,
            monthly_fee=10,
            contract_date=date(2026, 8, 5),
        ),
    ]

    data = _build(active_projects=projects)

    assert data.quarter_forecast.max.initial_fee == 100
    assert data.quarter_forecast.expected.initial_fee == 100
    assert data.quarter_forecast.min.initial_fee == 100


def test_quarter_forecast_is_all_zero_when_no_projects() -> None:
    data = _build()

    assert data.quarter_forecast.max.initial_fee == 0
    assert data.quarter_forecast.expected.initial_fee == 0
    assert data.quarter_forecast.min.initial_fee == 0


# --- コンディション🔴停滞リスク案件の一覧 ---


def test_stagnation_risk_projects_is_empty_when_none_at_risk() -> None:
    data = _build(as_of=WEEK_END)

    assert data.stagnation_risk_projects == ()


def test_stagnation_risk_projects_lists_projects_exceeding_threshold() -> None:
    outcomes = [ProjectOutcome(project_id="H1", total_contact_count=10, is_won=True)]
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="商談中(B)",
            total_contact_count=16,  # 平均10 * 1.5 = 15 を超過
            last_action_date=WEEK_END,
        ),
    ]

    data = _build(active_projects=projects, historical_outcomes=outcomes, as_of=WEEK_END)

    assert len(data.stagnation_risk_projects) == 1
    assert data.stagnation_risk_projects[0].client_name == "株式会社A"


def test_stagnation_risk_projects_excludes_lost_and_cancelled_projects() -> None:
    outcomes = [ProjectOutcome(project_id="H1", total_contact_count=10, is_won=True)]
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="失注",  # ACTIVE_STATUSESに含まれないため対象外
            total_contact_count=100,
            last_action_date=WEEK_END,
        ),
    ]

    data = _build(active_projects=projects, historical_outcomes=outcomes, as_of=WEEK_END)

    assert data.stagnation_risk_projects == ()


# --- generate_weekly_report_text ---


def test_generate_weekly_report_text_renders_all_sections() -> None:
    outcomes = [ProjectOutcome(project_id="H1", total_contact_count=10, is_won=True)]
    proposals = [
        ProposalRecord(project_id=f"P{i}", meeting_number=1, services=frozenset({"サービスX"}), is_won=True)
        for i in range(3)
    ]
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            initial_fee=100000,
            monthly_fee=10000,
            contract_date=date(2026, 8, 5),
        ),
        WeeklyProjectRecord(
            project_id="P2",
            client_name="株式会社B",
            assignee="鈴木",
            status="商談中(B)",
            total_contact_count=16,
            last_action_date=WEEK_END,
        ),
    ]

    member_actions = [
        MemberActionRecord(project_id="P1", member="佐藤", action_type="テレアポ", action_date=WEEK_END),
    ]

    data = _build(
        active_projects=projects,
        historical_outcomes=outcomes,
        proposal_records=proposals,
        member_actions=member_actions,
        monthly_target=RevenueTarget(initial_fee=1000000, mrr=100000),
        quarter_target=RevenueTarget(initial_fee=3000000, mrr=300000),
        as_of=WEEK_END,
    )
    text = generate_weekly_report_text(data)

    assert "2026-08-03" in text
    assert "2026-08-07" in text
    assert "100,000円" in text
    assert "10,000円" in text
    assert "10.0%" in text  # 月次進捗率
    assert "10.0回" in text  # 全社平均受注接触回数
    assert "1回目商談 × サービスX" in text
    assert "🚀 Max" in text
    assert "🎯 Expected" in text
    assert "🛡 Min" in text
    assert "株式会社B" in text
    assert "佐藤: 総合スコア" in text
    assert "鈴木: 総合スコア" in text


def test_generate_weekly_report_text_splits_initial_fee_and_mrr_progress_into_separate_lines() -> None:
    """初期費用とMRRの進捗は同じ行に詰め込まず、別々の行として出力すること。"""
    projects = [
        WeeklyProjectRecord(
            project_id="P1",
            client_name="株式会社A",
            assignee="佐藤",
            status="契約済",
            initial_fee=500000,
            monthly_fee=50000,
            contract_date=date(2026, 8, 5),
        ),
    ]

    data = _build(
        active_projects=projects,
        monthly_target=RevenueTarget(initial_fee=1000000, mrr=100000),
        as_of=WEEK_END,
    )
    text = generate_weekly_report_text(data)
    lines = text.splitlines()

    initial_fee_line = next(line for line in lines if "月次目標（初期費用）" in line)
    mrr_line = next(line for line in lines if "月次目標（MRR）" in line)

    assert initial_fee_line != mrr_line
    assert "MRR" not in initial_fee_line
    assert "初期費用" not in mrr_line


def test_generate_weekly_report_text_renders_placeholder_when_all_sections_are_empty() -> None:
    data = _build(as_of=WEEK_END)

    text = generate_weekly_report_text(data)

    assert "未確定（受注実績なし）" in text
    assert "サンプル数が十分な勝ちパターンはありません" in text
    assert "メンバー別パフォーマンスデータはありません" in text
    assert "🔴停滞リスク案件はありません" in text
    assert "目標未設定" in text


def test_generate_weekly_report_text_raises_readable_error_on_broken_template(
    tmp_path: Path,
) -> None:
    """テンプレートのプレースホルダが不正な場合、無言のKeyErrorではなく
    分かりやすいエラーメッセージのValueErrorになること（非エンジニアの運用担当者が
    テンプレートを編集する前提のため）。
    """
    broken_template = tmp_path / "broken_weekly_report.txt"
    broken_template.write_text("週報 {week_start} {存在しないプレースホルダ}", encoding="utf-8")
    data = _build(as_of=WEEK_END)

    with pytest.raises(ValueError, match="存在しないプレースホルダ"):
        generate_weekly_report_text(data, template_path=broken_template)
