"""06_営業分析ロジック「クオーター着地予測」の検証。"""

from __future__ import annotations

import itertools
import json
import logging
from pathlib import Path

from src.analytics.forecast import (
    DEFAULT_CONFIDENCE_WIN_RATES,
    ForecastAmount,
    ForecastProject,
    QuarterForecast,
    forecast_quarter,
    load_confidence_win_rates,
)


def test_forecast_quarter_empty_input_returns_all_zero() -> None:
    result = forecast_quarter([])

    assert result == QuarterForecast(
        max=ForecastAmount(initial_fee=0.0, mrr=0.0),
        expected=ForecastAmount(initial_fee=0.0, mrr=0.0),
        min=ForecastAmount(initial_fee=0.0, mrr=0.0),
    )


def test_forecast_quarter_confirmed_contracts_count_in_all_scenarios() -> None:
    projects = [
        ForecastProject(
            project_id="P1", confidence="C", status="契約済", initial_fee=100, monthly_fee=10
        ),
    ]

    result = forecast_quarter(projects, confidence_win_rates={})

    assert result.max == ForecastAmount(initial_fee=100, mrr=10)
    assert result.expected == ForecastAmount(initial_fee=100, mrr=10)
    assert result.min == ForecastAmount(initial_fee=100, mrr=10)


def test_forecast_quarter_max_includes_only_pending_s_and_a_rank() -> None:
    # B・Cの金額は、加重後のExpectedがMaxの単純合計(300)を超えない範囲に抑えている
    # （Expected側がMaxを超えるケースはBLOCKER2の不変条件テストで別途検証する）。
    projects = [
        ForecastProject(project_id="P1", confidence="S", status="商談中(B)", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence="A", status="提案中", initial_fee=200, monthly_fee=20),
        ForecastProject(project_id="P3", confidence="B", status="提案中", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P4", confidence="C", status="初回接触", initial_fee=1000, monthly_fee=100),
    ]

    result = forecast_quarter(projects)

    assert result.max == ForecastAmount(initial_fee=300, mrr=30)


def test_forecast_quarter_min_includes_only_pending_s_rank() -> None:
    projects = [
        ForecastProject(project_id="P1", confidence="S", status="商談中(B)", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence="A", status="提案中", initial_fee=1000, monthly_fee=100),
    ]

    result = forecast_quarter(projects)

    assert result.min == ForecastAmount(initial_fee=100, mrr=10)


def test_forecast_quarter_expected_weights_pending_by_confidence_win_rate() -> None:
    projects = [
        ForecastProject(project_id="P1", confidence="S", status="提案中", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence="B", status="提案中", initial_fee=200, monthly_fee=20),
    ]

    result = forecast_quarter(projects, confidence_win_rates={"S": 0.8, "B": 0.2})

    assert result.expected == ForecastAmount(initial_fee=100 * 0.8 + 200 * 0.2, mrr=10 * 0.8 + 20 * 0.2)


def test_forecast_quarter_pending_project_with_no_confidence_contributes_zero_to_expected() -> None:
    projects = [
        ForecastProject(project_id="P1", confidence=None, status="初回接触", initial_fee=500, monthly_fee=50),
    ]

    result = forecast_quarter(projects)

    assert result.expected == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.max == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.min == ForecastAmount(initial_fee=0.0, mrr=0.0)


def test_load_confidence_win_rates_falls_back_to_defaults_when_file_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist.json"

    result = load_confidence_win_rates(missing_path)

    assert result == DEFAULT_CONFIDENCE_WIN_RATES


def test_load_confidence_win_rates_reads_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "analytics_thresholds.json"
    config_path.write_text(
        json.dumps({"confidence_win_rates": {"S": 0.9}}),
        encoding="utf-8",
    )

    result = load_confidence_win_rates(config_path)

    assert result["S"] == 0.9
    assert result["A"] == DEFAULT_CONFIDENCE_WIN_RATES["A"]


def test_load_confidence_win_rates_default_path_matches_repo_config() -> None:
    result = load_confidence_win_rates()

    assert result == DEFAULT_CONFIDENCE_WIN_RATES


# --- BLOCKER1: 失注・解約案件がpendingに含まれてしまうバグの回帰テスト ---


def test_forecast_quarter_excludes_lost_projects_from_all_scenarios() -> None:
    projects = [
        ForecastProject(
            project_id="P1", confidence="B", status="失注", initial_fee=1000, monthly_fee=100
        ),
    ]

    result = forecast_quarter(projects)

    assert result.max == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.expected == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.min == ForecastAmount(initial_fee=0.0, mrr=0.0)


def test_forecast_quarter_excludes_cancelled_projects_from_all_scenarios() -> None:
    projects = [
        ForecastProject(
            project_id="P1", confidence="A", status="解約", initial_fee=1000, monthly_fee=100
        ),
    ]

    result = forecast_quarter(projects)

    assert result.max == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.expected == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.min == ForecastAmount(initial_fee=0.0, mrr=0.0)


def test_forecast_quarter_lost_and_cancelled_do_not_affect_active_projects_amount() -> None:
    projects = [
        ForecastProject(
            project_id="P1", confidence="S", status="商談中(B)", initial_fee=100, monthly_fee=10
        ),
        ForecastProject(
            project_id="P2", confidence="A", status="失注", initial_fee=99999, monthly_fee=9999
        ),
        ForecastProject(
            project_id="P3", confidence="S", status="解約", initial_fee=99999, monthly_fee=9999
        ),
    ]

    # confidence_win_rates={"S": 1.0}を指定し、Min側のExpectedキャップ補正
    # （BLOCKER2）の影響を受けずに「失注・解約が計上されないこと」だけを検証する。
    result = forecast_quarter(projects, confidence_win_rates={"S": 1.0})

    assert result.max == ForecastAmount(initial_fee=100, mrr=10)
    assert result.min == ForecastAmount(initial_fee=100, mrr=10)


# --- BLOCKER2: Max ≧ Expected ≧ Min の不変条件 ---


def test_forecast_quarter_max_is_raised_to_expected_when_expected_would_exceed_it() -> None:
    """B/Cランクが多いパイプラインでは、Expectedの加重合計がMaxの単純合計(S+A)を
    上回りうる。この場合Maxが「最大値」であることを保証するため引き上げられる。"""
    projects = (
        [ForecastProject(project_id="S1", confidence="S", status="提案中", initial_fee=10, monthly_fee=1)]
        + [ForecastProject(project_id="A1", confidence="A", status="提案中", initial_fee=10, monthly_fee=1)]
        + [
            ForecastProject(project_id=f"B{i}", confidence="B", status="提案中", initial_fee=10, monthly_fee=1)
            for i in range(5)
        ]
        + [
            ForecastProject(project_id=f"C{i}", confidence="C", status="提案中", initial_fee=10, monthly_fee=1)
            for i in range(10)
        ]
    )

    result = forecast_quarter(projects, confidence_win_rates={"S": 0.8, "A": 0.5, "B": 0.2, "C": 0.05})

    # naiveなMax(S+Aのみ)=20だが、Expected = 10*0.8+10*0.5+50*0.2+100*0.05 = 28 の方が大きいため
    # Maxは28まで引き上げられる。
    assert result.expected.initial_fee == 28.0
    assert result.max.initial_fee == 28.0
    assert result.max.initial_fee >= result.expected.initial_fee


def test_forecast_quarter_invariant_max_gte_expected_gte_min_across_many_combinations() -> None:
    """確度・件数・金額の様々な組み合わせでMax ≧ Expected ≧ Minが常に成立することを検証する。"""
    confidences = ["S", "A", "B", "C", None]
    win_rates = {"S": 0.8, "A": 0.5, "B": 0.2, "C": 0.05}

    for counts in itertools.product([0, 1, 3], repeat=len(confidences)):
        projects = []
        for confidence, count in zip(confidences, counts):
            for i in range(count):
                projects.append(
                    ForecastProject(
                        project_id=f"{confidence}-{i}",
                        confidence=confidence,
                        status="提案中",
                        initial_fee=100.0,
                        monthly_fee=10.0,
                    )
                )

        result = forecast_quarter(projects, confidence_win_rates=win_rates)

        assert result.max.initial_fee >= result.expected.initial_fee >= result.min.initial_fee
        assert result.max.mrr >= result.expected.mrr >= result.min.mrr


# --- 未知の確度値の検知 ---


def test_forecast_quarter_logs_warning_for_unknown_confidence(caplog) -> None:
    projects = [
        ForecastProject(
            project_id="P1", confidence="Z", status="提案中", initial_fee=100, monthly_fee=10
        ),
    ]

    with caplog.at_level(logging.WARNING):
        result = forecast_quarter(projects)

    assert result.expected == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert any("未知の確度値" in record.getMessage() for record in caplog.records)
