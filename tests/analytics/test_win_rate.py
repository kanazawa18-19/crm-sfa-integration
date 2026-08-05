"""06_営業分析ロジック「段階別平均受注率」「何回目以内の受注率」「平均受注接触回数」の検証。"""

from __future__ import annotations

from src.analytics.win_rate import (
    ProjectOutcome,
    average_won_contact_count,
    best_win_rate_threshold,
    cumulative_win_rates,
    stage_win_rates,
)


def _outcome(project_id: str, total_contact_count: int, is_won: bool) -> ProjectOutcome:
    return ProjectOutcome(project_id=project_id, total_contact_count=total_contact_count, is_won=is_won)


def test_stage_win_rates_empty_input_returns_empty_dict() -> None:
    assert stage_win_rates([]) == {}


def test_stage_win_rates_computed_against_projects_reaching_each_stage() -> None:
    projects = [
        _outcome("P1", total_contact_count=1, is_won=False),
        _outcome("P2", total_contact_count=2, is_won=True),
        _outcome("P3", total_contact_count=3, is_won=True),
    ]

    result = stage_win_rates(projects)

    # stage1: 3件中1件受注(P2,P3到達だがP1未受注) -> 実際にはP1,P2,P3全て>=1到達 = 3件中2件受注
    assert result[1] == 2 / 3
    # stage2: P2,P3が到達(>=2) 2件中2件受注
    assert result[2] == 2 / 2
    # stage3: P3のみ到達 1件中1件受注
    assert result[3] == 1 / 1


def test_stage_win_rates_respects_explicit_max_stage() -> None:
    projects = [_outcome("P1", total_contact_count=5, is_won=True)]

    result = stage_win_rates(projects, max_stage=2)

    assert set(result.keys()) == {1, 2}


def test_cumulative_win_rates_empty_input_returns_empty_dict() -> None:
    assert cumulative_win_rates([]) == {}


def test_cumulative_win_rates_computed_against_projects_concluded_within_each_stage() -> None:
    projects = [
        _outcome("P1", total_contact_count=1, is_won=True),
        _outcome("P2", total_contact_count=3, is_won=False),
        _outcome("P3", total_contact_count=7, is_won=True),
    ]

    result = cumulative_win_rates(projects)

    # 1回以内: P1のみ(1件中1件受注)
    assert result[1] == 1 / 1
    # 3回以内: P1,P2(2件中1件受注)
    assert result[3] == 1 / 2
    # 7回以内: 全件(3件中2件受注)
    assert result[7] == 2 / 3


def test_best_win_rate_threshold_returns_none_for_empty_input() -> None:
    assert best_win_rate_threshold({}) is None


def test_best_win_rate_threshold_picks_stage_with_largest_marginal_increase() -> None:
    # 0->1: +0.1 / 1->6: +0.1 / 6->7: +0.72(最大の伸び)
    rates = {1: 0.1, 6: 0.2, 7: 0.92}

    assert best_win_rate_threshold(rates) == 7


def test_average_won_contact_count_returns_none_when_no_won_projects() -> None:
    projects = [_outcome("P1", total_contact_count=3, is_won=False)]

    assert average_won_contact_count(projects) is None


def test_average_won_contact_count_averages_only_won_projects() -> None:
    projects = [
        _outcome("P1", total_contact_count=4, is_won=True),
        _outcome("P2", total_contact_count=6, is_won=True),
        _outcome("P3", total_contact_count=100, is_won=False),
    ]

    assert average_won_contact_count(projects) == 5.0
