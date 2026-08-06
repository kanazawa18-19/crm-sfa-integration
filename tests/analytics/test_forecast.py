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
            project_id="P1", confidence="C", status="契約", initial_fee=100, monthly_fee=10
        ),
    ]

    result = forecast_quarter(projects, confidence_win_rates={})

    assert result.max == ForecastAmount(initial_fee=100, mrr=10)
    assert result.expected == ForecastAmount(initial_fee=100, mrr=10)
    assert result.min == ForecastAmount(initial_fee=100, mrr=10)


def test_forecast_quarter_confirmed_statuses_include_both_facility_contract_and_contract() -> None:
    """「施設契約」「契約」の両方が契約済扱いになること（複数値対応の回帰確認）。"""
    projects = [
        ForecastProject(
            project_id="P1", confidence=None, status="施設契約", initial_fee=100, monthly_fee=10
        ),
        ForecastProject(
            project_id="P2", confidence=None, status="契約", initial_fee=200, monthly_fee=20
        ),
    ]

    result = forecast_quarter(projects, confidence_win_rates={})

    assert result.max == ForecastAmount(initial_fee=300, mrr=30)
    assert result.expected == ForecastAmount(initial_fee=300, mrr=30)
    assert result.min == ForecastAmount(initial_fee=300, mrr=30)


def test_forecast_quarter_max_includes_only_pending_a_rank() -> None:
    # B・C・Dの金額は、加重後のExpectedがMaxの単純合計(Aランクのみ=100)を超えない範囲に
    # 抑えている（Expected側がMaxを超えるケースはBLOCKER2の不変条件テストで別途検証する）。
    projects = [
        ForecastProject(project_id="P1", confidence="A", status="アポ", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence="B", status="リスケ", initial_fee=10, monthly_fee=1),
        ForecastProject(project_id="P3", confidence="C", status="Cヨミ", initial_fee=10, monthly_fee=1),
        ForecastProject(project_id="P4", confidence="D", status="Dヨミ", initial_fee=10, monthly_fee=1),
    ]

    result = forecast_quarter(projects)

    assert result.max == ForecastAmount(initial_fee=100, mrr=10)


def test_forecast_quarter_min_includes_no_pending_projects() -> None:
    """MIN_SCENARIO_RANKSは空集合のため、Aランクの未契約案件も含めずMin＝契約確定分のみ。"""
    projects = [
        ForecastProject(project_id="P1", confidence="A", status="アポ", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence="B", status="リスケ", initial_fee=1000, monthly_fee=100),
    ]

    result = forecast_quarter(projects)

    assert result.min == ForecastAmount(initial_fee=0, mrr=0)


def test_forecast_quarter_expected_weights_pending_by_confidence_win_rate() -> None:
    projects = [
        ForecastProject(project_id="P1", confidence="A", status="アポ", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence="B", status="リスケ", initial_fee=200, monthly_fee=20),
    ]

    result = forecast_quarter(projects, confidence_win_rates={"A": 0.8, "B": 0.2})

    assert result.expected == ForecastAmount(initial_fee=100 * 0.8 + 200 * 0.2, mrr=10 * 0.8 + 20 * 0.2)


def test_forecast_quarter_pending_project_with_no_confidence_contributes_zero_to_expected() -> None:
    projects = [
        ForecastProject(project_id="P1", confidence=None, status="アポ", initial_fee=500, monthly_fee=50),
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
        json.dumps({"confidence_win_rates": {"A": 0.9}}),
        encoding="utf-8",
    )

    result = load_confidence_win_rates(config_path)

    assert result["A"] == 0.9
    assert result["B"] == DEFAULT_CONFIDENCE_WIN_RATES["B"]


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
            project_id="P0", confidence=None, status="契約", initial_fee=100, monthly_fee=10
        ),
        ForecastProject(
            project_id="P1", confidence="A", status="アポ", initial_fee=100, monthly_fee=10
        ),
        ForecastProject(
            project_id="P2", confidence="B", status="失注", initial_fee=99999, monthly_fee=9999
        ),
        ForecastProject(
            project_id="P3", confidence="A", status="解約", initial_fee=99999, monthly_fee=9999
        ),
    ]

    # confidence_win_rates={"A": 1.0}を指定し、Max側のExpectedキャップ補正
    # （BLOCKER2）の影響を受けずに「失注・解約が計上されないこと」だけを検証する。
    # Min（MIN_SCENARIO_RANKSは空集合）は契約確定分のみで、P0の契約確定分(100/10)によって
    # 失注・解約案件(99999/9999)が漏れ込んでいないことを検証できる。
    result = forecast_quarter(projects, confidence_win_rates={"A": 1.0})

    assert result.max == ForecastAmount(initial_fee=200, mrr=20)
    assert result.min == ForecastAmount(initial_fee=100, mrr=10)


# --- BLOCKER2: Max ≧ Expected ≧ Min の不変条件 ---


def test_forecast_quarter_max_is_raised_to_expected_when_expected_would_exceed_it() -> None:
    """B/C/Dランクが多いパイプラインでは、Expectedの加重合計がMaxの単純合計(Aランクのみ)を
    上回りうる。この場合Maxが「最大値」であることを保証するため引き上げられる。"""
    projects = (
        [ForecastProject(project_id="A1", confidence="A", status="アポ", initial_fee=10, monthly_fee=1)]
        + [ForecastProject(project_id="B1", confidence="B", status="アポ", initial_fee=10, monthly_fee=1)]
        + [
            ForecastProject(project_id=f"C{i}", confidence="C", status="アポ", initial_fee=10, monthly_fee=1)
            for i in range(5)
        ]
        + [
            ForecastProject(project_id=f"D{i}", confidence="D", status="アポ", initial_fee=10, monthly_fee=1)
            for i in range(10)
        ]
    )

    result = forecast_quarter(projects, confidence_win_rates={"A": 0.8, "B": 0.5, "C": 0.2, "D": 0.05})

    # naiveなMax(Aランクのみ)=10だが、Expected = 10*0.8+10*0.5+50*0.2+100*0.05 = 28 の方が大きいため
    # Maxは28まで引き上げられる。
    assert result.expected.initial_fee == 28.0
    assert result.max.initial_fee == 28.0
    assert result.max.initial_fee >= result.expected.initial_fee


# --- BLOCKER3: MIN_SCENARIO_RANKSが空集合になったことの回帰テスト ---
# （MAX_SCENARIO_RANKSとMIN_SCENARIO_RANKSが両方{"A"}だった旧実装では、後処理の
# キャップ処理max(R, E)/min(R, E)によりMaxまたはMinのどちらかが必ずExpectedと
# 完全一致してしまい、3段階シミュレーションが退化していた。）


def test_forecast_quarter_min_never_equals_expected_for_a_rank_only_pipeline() -> None:
    """Aランクのみ・B/C/D不在のパイプラインでは、旧実装ではMin=Expectedに完全一致して
    いた（Aランク受注率が1.0未満のため）。MIN_SCENARIO_RANKSを空集合にしたことで、
    Minは契約確定分のみとなりExpectedより明確に低くなる。"""
    projects = [
        ForecastProject(
            project_id="P1", confidence="A", status="アポ", initial_fee=1500, monthly_fee=150
        ),
    ]

    result = forecast_quarter(projects, confidence_win_rates={"A": 0.8})

    assert result.max == ForecastAmount(initial_fee=1500.0, mrr=150.0)
    assert result.expected == ForecastAmount(initial_fee=1200.0, mrr=120.0)
    assert result.min == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.min.initial_fee != result.expected.initial_fee
    assert result.min.mrr != result.expected.mrr


def test_forecast_quarter_max_equals_expected_when_a_rank_absent_but_min_stays_distinct() -> None:
    """B/Cランクのみ・Aランク不在のパイプラインでは、max_pendingが空になるため
    Max算出値（契約確定分のみ）はExpectedを下回り、後処理でExpectedまで引き上げ
    られてMax=Expectedとなる（これはA不在時の妥当な挙動であり、退化ではない）。
    一方Minは契約確定分のみ（=0）でExpectedとは明確に異なることを確認する。"""
    projects = [
        ForecastProject(
            project_id="P1", confidence="B", status="アポ", initial_fee=1000, monthly_fee=100
        ),
        ForecastProject(
            project_id="P2", confidence="C", status="Cヨミ", initial_fee=1000, monthly_fee=100
        ),
    ]

    result = forecast_quarter(projects, confidence_win_rates={"B": 0.5, "C": 0.1})

    assert result.expected == ForecastAmount(initial_fee=600.0, mrr=60.0)
    assert result.max == ForecastAmount(initial_fee=600.0, mrr=60.0)
    assert result.min == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.min.initial_fee != result.expected.initial_fee


def test_forecast_quarter_invariant_max_gte_expected_gte_min_across_many_combinations() -> None:
    """確度・件数・金額の様々な組み合わせでMax ≧ Expected ≧ Minが常に成立することを検証する。"""
    confidences = ["A", "B", "C", "D", None]
    win_rates = {"A": 0.8, "B": 0.5, "C": 0.2, "D": 0.05}

    for counts in itertools.product([0, 1, 3], repeat=len(confidences)):
        projects = []
        for confidence, count in zip(confidences, counts):
            for i in range(count):
                projects.append(
                    ForecastProject(
                        project_id=f"{confidence}-{i}",
                        confidence=confidence,
                        status="アポ",
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
            project_id="P1", confidence="Z", status="アポ", initial_fee=100, monthly_fee=10
        ),
    ]

    with caplog.at_level(logging.WARNING):
        result = forecast_quarter(projects)

    assert result.expected == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert any("未知の確度値" in record.getMessage() for record in caplog.records)
