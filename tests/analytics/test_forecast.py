"""06_営業分析ロジック「クオーター着地予測」の検証。"""

from __future__ import annotations

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


def test_forecast_quarter_max_includes_pending_with_a_or_b_yomi_status() -> None:
    """Max（楽観）は営業ステータスが「Aヨミ」「Bヨミ」の未契約案件のみを全額計上する
    （2026-08-14、確度ではなく営業ステータスの値ベースに変更）。確度は無関係。"""
    projects = [
        ForecastProject(project_id="P1", confidence=None, status="Aヨミ", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence=None, status="Bヨミ", initial_fee=50, monthly_fee=5),
        ForecastProject(project_id="P3", confidence="A", status="Cヨミ", initial_fee=10, monthly_fee=1),
        ForecastProject(project_id="P4", confidence="A", status="Dヨミ", initial_fee=10, monthly_fee=1),
        ForecastProject(project_id="P5", confidence="A", status="アポ", initial_fee=10, monthly_fee=1),
    ]

    result = forecast_quarter(projects, confidence_win_rates={"A": 0.1})

    # 確度Aだが営業ステータスがCヨミ/Dヨミ/アポのP3〜P5はMaxに加算されない
    # （AヨミでもBヨミでもないため）。P1(Aヨミ)+P2(Bヨミ)=150/15のみ。
    assert result.max == ForecastAmount(initial_fee=150, mrr=15)


def test_forecast_quarter_min_includes_a_yomi_kotou_juchu_and_trial_regardless_of_confidence() -> None:
    """Min（悲観）は営業ステータスが「Aヨミ」「口頭受注」「トライアル」の未契約案件を
    確度を問わず全額計上する（2026-08-14方針変更、金沢さん確認済み）。"""
    projects = [
        ForecastProject(project_id="P1", confidence=None, status="Aヨミ", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence="D", status="口頭受注", initial_fee=50, monthly_fee=5),
        ForecastProject(project_id="P3", confidence=None, status="トライアル", initial_fee=20, monthly_fee=2),
    ]

    result = forecast_quarter(projects, confidence_win_rates={})

    assert result.min == ForecastAmount(initial_fee=170, mrr=17)


def test_forecast_quarter_min_excludes_b_yomi_and_other_active_statuses() -> None:
    """MinはBヨミ・その他の営業ステータスは含めない（Aヨミ・口頭受注・トライアルのみ）。"""
    projects = [
        ForecastProject(project_id="P1", confidence="A", status="Bヨミ", initial_fee=100, monthly_fee=10),
        ForecastProject(project_id="P2", confidence="A", status="アポ", initial_fee=1000, monthly_fee=100),
        ForecastProject(project_id="P3", confidence="A", status="リスケ", initial_fee=1000, monthly_fee=100),
    ]

    result = forecast_quarter(projects, confidence_win_rates={"A": 0.01})

    assert result.min == ForecastAmount(initial_fee=0, mrr=0)


def test_forecast_quarter_min_includes_no_pending_projects_when_none_qualify() -> None:
    """営業ステータスがMIN_SCENARIO_STATUSES対象外の未契約案件しか無ければMin＝契約確定分のみ。"""
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

    # confidence_win_rates={"A": 1.0}を指定するが、Max/Minはもはや確度に影響されない
    # （営業ステータス値ベースのため）。「失注・解約が計上されないこと」だけを検証する。
    # P1(アポ)はMAX_SCENARIO_STATUSES対象外のためMaxには含まれない。
    result = forecast_quarter(projects, confidence_win_rates={"A": 1.0})

    assert result.max == ForecastAmount(initial_fee=100, mrr=10)
    assert result.min == ForecastAmount(initial_fee=100, mrr=10)


# --- Max/Expected/Minが互いに独立していることの確認 ---
# （2026-08-14、金沢さんの判断でMax ≧ Expected ≧ Minの不変条件キャップを撤廃した。
# 以前はMaxがExpectedを下回れば引き上げ、MinがExpectedを上回れば引き下げていたが、
# 現在は3者とも自身の算出ロジックの結果をそのまま返す。Min > ExpectedやMax < Expected
# が起こり得ることを、以下のテストで明示的に固定化する。）


def test_forecast_quarter_max_can_be_lower_than_expected_when_no_a_or_b_yomi_pending() -> None:
    """Aヨミ・Bヨミの未契約案件が無いパイプラインでは、Max（契約確定分のみ）が
    Expected（確度による加重）を下回ることがある。以前はExpectedまで引き上げる
    キャップがあったが、現在はキャップせずそのまま返す。"""
    projects = [
        ForecastProject(
            project_id="P1", confidence="A", status="アポ", initial_fee=1500, monthly_fee=150
        ),
    ]

    result = forecast_quarter(projects, confidence_win_rates={"A": 0.8})

    assert result.expected == ForecastAmount(initial_fee=1200.0, mrr=120.0)
    assert result.max == ForecastAmount(initial_fee=0.0, mrr=0.0)
    assert result.max.initial_fee < result.expected.initial_fee


def test_forecast_quarter_min_can_exceed_expected_when_a_yomi_confidence_is_low() -> None:
    """Aヨミ案件はMinには確度を問わず全額計上されるが、Expectedへの寄与は確度による
    加重（ここではD、win_rate=0.05）のみ。そのためMinがExpectedを上回ることがある。
    以前はExpectedまで引き下げるキャップがあったが、現在はキャップせずそのまま返す。"""
    projects = [
        ForecastProject(
            project_id="P1", confidence="D", status="Aヨミ", initial_fee=1000, monthly_fee=100
        ),
    ]

    result = forecast_quarter(projects, confidence_win_rates={"D": 0.05})

    assert result.expected == ForecastAmount(initial_fee=50.0, mrr=5.0)
    assert result.min == ForecastAmount(initial_fee=1000.0, mrr=100.0)
    assert result.min.initial_fee > result.expected.initial_fee


def test_forecast_quarter_max_min_and_expected_are_computed_independently_across_combinations() -> None:
    """営業ステータス（Max/Minの判定基準）・確度（Expectedの判定基準）の様々な組み合わせで、
    forecast_quarter()が例外を送出せず、各シナリオがそれぞれの定義通りに算出されることを
    確認する（Max ≧ Expected ≧ Minの大小関係は保証しないため、ここでは検証しない）。"""
    statuses = ["Aヨミ", "Bヨミ", "Cヨミ", "口頭受注", "トライアル", "アポ"]
    confidences = ["A", "B", "C", "D", None]
    win_rates = {"A": 0.8, "B": 0.5, "C": 0.2, "D": 0.05}

    for status in statuses:
        for confidence in confidences:
            for count in (0, 1, 3):
                projects = [
                    ForecastProject(
                        project_id=f"{status}-{confidence}-{i}",
                        confidence=confidence,
                        status=status,
                        initial_fee=100.0,
                        monthly_fee=10.0,
                    )
                    for i in range(count)
                ]

                result = forecast_quarter(projects, confidence_win_rates=win_rates)

                expected_max = 100.0 * count if status in {"Aヨミ", "Bヨミ"} else 0.0
                expected_min = 100.0 * count if status in {"Aヨミ", "口頭受注", "トライアル"} else 0.0
                expected_expected = 100.0 * count * win_rates.get(confidence, 0.0)

                assert result.max.initial_fee == expected_max
                assert result.min.initial_fee == expected_min
                assert result.expected.initial_fee == expected_expected


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
