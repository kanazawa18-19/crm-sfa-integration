"""06_営業分析ロジック「コンディション自動判定」の検証。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from src.analytics.condition import (
    Condition,
    ConditionThresholds,
    load_condition_thresholds,
    judge_condition,
)

AS_OF = date(2026, 8, 5)


def test_good_when_within_stale_days_and_at_or_below_average() -> None:
    result = judge_condition(
        last_action_date=date(2026, 7, 25),  # 11日前
        total_contact_count=5,
        average_contact_count=5,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.GOOD


def test_good_boundary_exactly_14_days_is_still_good() -> None:
    result = judge_condition(
        last_action_date=date(2026, 7, 22),  # ちょうど14日前
        total_contact_count=1,
        average_contact_count=5,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.GOOD


def test_needs_follow_up_when_stale_days_exceeded() -> None:
    result = judge_condition(
        last_action_date=date(2026, 7, 21),  # 15日前
        total_contact_count=1,
        average_contact_count=5,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.NEEDS_FOLLOW_UP


def test_needs_follow_up_when_last_action_date_is_none() -> None:
    result = judge_condition(
        last_action_date=None,
        total_contact_count=1,
        average_contact_count=5,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.NEEDS_FOLLOW_UP


def test_stagnation_risk_when_over_1_5x_average_and_not_won() -> None:
    result = judge_condition(
        last_action_date=date(2026, 8, 4),  # 直近
        total_contact_count=16,  # 平均10 * 1.5 = 15を超過
        average_contact_count=10,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.STAGNATION_RISK


def test_stagnation_risk_boundary_exactly_1_5x_is_not_stagnation() -> None:
    result = judge_condition(
        last_action_date=date(2026, 8, 4),
        total_contact_count=15,  # ちょうど1.5倍 -> 超過ではない
        average_contact_count=10,
        is_won=False,
        as_of=AS_OF,
    )

    assert result != Condition.STAGNATION_RISK


def test_stagnation_risk_does_not_apply_to_won_projects() -> None:
    result = judge_condition(
        last_action_date=date(2026, 7, 1),  # 古いので要フォロー側に落ちる
        total_contact_count=100,
        average_contact_count=10,
        is_won=True,
        as_of=AS_OF,
    )

    assert result != Condition.STAGNATION_RISK


def test_gray_zone_falls_back_to_needs_follow_up() -> None:
    """14日以内・平均超〜1.5倍以内・未契約というグレーゾーンは安全側でNEEDS_FOLLOW_UPとする。"""
    result = judge_condition(
        last_action_date=date(2026, 8, 4),
        total_contact_count=12,  # 平均10より多いが1.5倍(15)以下
        average_contact_count=10,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.NEEDS_FOLLOW_UP


def test_zero_contact_count_and_zero_average_is_good() -> None:
    result = judge_condition(
        last_action_date=date(2026, 8, 5),
        total_contact_count=0,
        average_contact_count=0,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.GOOD


def test_custom_thresholds_are_respected() -> None:
    thresholds = ConditionThresholds(stale_days=7, stagnation_multiplier=2.0)

    result = judge_condition(
        last_action_date=date(2026, 7, 30),  # 6日前 -> stale_days=7以内
        total_contact_count=10,  # 平均10以下
        average_contact_count=10,
        is_won=False,
        as_of=AS_OF,
        thresholds=thresholds,
    )

    assert result == Condition.GOOD

    # stale_days=7を8日超過 -> デフォルト(14日)なら順調のはずがカスタム閾値ではNEEDS_FOLLOW_UPになる
    stale_result = judge_condition(
        last_action_date=date(2026, 7, 25),  # 11日前
        total_contact_count=10,
        average_contact_count=10,
        is_won=False,
        as_of=AS_OF,
        thresholds=thresholds,
    )

    assert stale_result == Condition.NEEDS_FOLLOW_UP


def test_load_condition_thresholds_falls_back_to_defaults_when_file_missing(tmp_path: Path) -> None:
    missing_path = tmp_path / "does_not_exist.json"

    result = load_condition_thresholds(missing_path)

    assert result == ConditionThresholds(stale_days=14, stagnation_multiplier=1.5)


def test_load_condition_thresholds_reads_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "analytics_thresholds.json"
    config_path.write_text(
        json.dumps({"condition": {"stale_days": 21, "stagnation_multiplier": 2.0}}),
        encoding="utf-8",
    )

    result = load_condition_thresholds(config_path)

    assert result == ConditionThresholds(stale_days=21, stagnation_multiplier=2.0)


def test_load_condition_thresholds_default_path_matches_repo_config() -> None:
    """リポジトリに同梱のconfig/analytics_thresholds.jsonが仕様書の初期値通りであることを確認する。"""
    result = load_condition_thresholds()

    assert result == ConditionThresholds(stale_days=14, stagnation_multiplier=1.5)


# --- WARN3: average_contact_countが0またはNone（全社平均未確定）の場合の挙動 ---


def test_average_contact_count_zero_does_not_trigger_stagnation_risk() -> None:
    """受注実績ゼロの立ち上げ期でaverage_contact_count=0にフォールバックさせても、
    接触1回の案件が軒並み🔴停滞リスクにならないことを確認する。"""
    result = judge_condition(
        last_action_date=date(2026, 8, 4),  # 直近 -> stale_daysの範囲内
        total_contact_count=1,
        average_contact_count=0,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.GOOD


def test_average_contact_count_zero_and_stale_falls_back_to_needs_follow_up() -> None:
    result = judge_condition(
        last_action_date=date(2026, 7, 1),  # stale_days超過
        total_contact_count=1,
        average_contact_count=0,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.NEEDS_FOLLOW_UP


def test_average_contact_count_none_does_not_trigger_stagnation_risk() -> None:
    """average_contact_count=Noneも同様に全社平均未確定として扱い、🔴停滞リスク判定をスキップする。"""
    result = judge_condition(
        last_action_date=date(2026, 8, 4),
        total_contact_count=100,
        average_contact_count=None,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.GOOD


def test_average_contact_count_none_and_stale_falls_back_to_needs_follow_up() -> None:
    result = judge_condition(
        last_action_date=date(2026, 7, 1),
        total_contact_count=100,
        average_contact_count=None,
        is_won=False,
        as_of=AS_OF,
    )

    assert result == Condition.NEEDS_FOLLOW_UP


# --- WARN4: is_wonだけでは失注・解約と進行中を区別できない（既知の制約） ---


def test_lost_or_cancelled_project_without_caller_side_filtering_remains_stagnation_risk() -> None:
    """judge_conditionは営業ステータスを見ないため、失注・解約案件もis_won=Falseとして
    渡されると🔴停滞リスクの判定対象になってしまう。本来は呼び出し側で失注・解約など
    既に決着した案件を判定対象からあらかじめ除外すべき（docstring参照）。
    この挙動自体は本関数の設計上の既知の制約であり、ここでは現状の挙動を明記する。"""
    result = judge_condition(
        last_action_date=date(2026, 8, 4),
        total_contact_count=100,  # 平均10 * 1.5 = 15を大幅に超過
        average_contact_count=10,
        is_won=False,  # 実際には「失注」「解約」でもis_won=Falseになる
        as_of=AS_OF,
    )

    assert result == Condition.STAGNATION_RISK
