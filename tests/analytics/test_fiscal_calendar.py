"""src/analytics/fiscal_calendar.py（会計年度＝期初12月・期末11月）の検証。"""

from __future__ import annotations

from datetime import date

from src.analytics.fiscal_calendar import (
    fiscal_half_range,
    fiscal_quarter_number,
    fiscal_quarter_range,
    fiscal_year_range,
    fiscal_year_start,
)


# --- fiscal_year_start：年またぎ判定 ------------------------------------------------------------


def test_fiscal_year_start_for_december_is_same_calendar_year() -> None:
    assert fiscal_year_start(date(2026, 12, 1)) == date(2026, 12, 1)


def test_fiscal_year_start_for_january_is_previous_calendar_year() -> None:
    # 2027年1月は「2026年12月に開始した会計年度」に属する（翌年12月ではない）。
    assert fiscal_year_start(date(2027, 1, 15)) == date(2026, 12, 1)


def test_fiscal_year_start_for_november_is_previous_calendar_year() -> None:
    assert fiscal_year_start(date(2027, 11, 30)) == date(2026, 12, 1)


# --- fiscal_quarter_range：各クオーター --------------------------------------------------------


def test_fiscal_quarter_range_q1_december() -> None:
    assert fiscal_quarter_range(date(2026, 12, 15)) == (date(2026, 12, 1), date(2027, 2, 28))


def test_fiscal_quarter_range_q1_january_rollover() -> None:
    """1月はQ1に属し、Q1の初日は前年12/1（年またぎのバグを検知する回帰テスト）。"""
    assert fiscal_quarter_range(date(2027, 1, 10)) == (date(2026, 12, 1), date(2027, 2, 28))


def test_fiscal_quarter_range_q1_february() -> None:
    assert fiscal_quarter_range(date(2027, 2, 28)) == (date(2026, 12, 1), date(2027, 2, 28))


def test_fiscal_quarter_range_q2_march_to_may() -> None:
    assert fiscal_quarter_range(date(2027, 4, 15)) == (date(2027, 3, 1), date(2027, 5, 31))


def test_fiscal_quarter_range_q3_june_to_august() -> None:
    assert fiscal_quarter_range(date(2026, 8, 15)) == (date(2026, 6, 1), date(2026, 8, 31))


def test_fiscal_quarter_range_q4_september_to_november() -> None:
    assert fiscal_quarter_range(date(2026, 11, 1)) == (date(2026, 9, 1), date(2026, 11, 30))


# --- fiscal_quarter_range：境界日（月末/月初） --------------------------------------------------


def test_fiscal_quarter_range_boundary_november_30_is_q4() -> None:
    """11/30は会計年度末日（Q4の末日）であり、12/1（翌年度Q1初日）とは別クオーターになる。"""
    assert fiscal_quarter_range(date(2026, 11, 30)) == (date(2026, 9, 1), date(2026, 11, 30))


def test_fiscal_quarter_range_boundary_december_1_is_next_fiscal_year_q1() -> None:
    assert fiscal_quarter_range(date(2026, 12, 1)) == (date(2026, 12, 1), date(2027, 2, 28))


def test_fiscal_quarter_range_boundary_february_28_29_vs_march_1() -> None:
    # うるう年でないため2027年2月末は28日。Q1最終日と、翌日3/1（Q2初日）で境界確認する。
    assert fiscal_quarter_range(date(2027, 2, 28)) == (date(2026, 12, 1), date(2027, 2, 28))
    assert fiscal_quarter_range(date(2027, 3, 1)) == (date(2027, 3, 1), date(2027, 5, 31))


def test_fiscal_quarter_range_q1_leap_year_february_ends_29() -> None:
    """2027年12月開始の会計年度のQ1は2028年2月まで含み、2028年はうるう年のため
    Q1の末日は2/29になる（非うるう年の2/28で決め打ちしていないことのピン留め）。"""
    assert fiscal_quarter_range(date(2028, 2, 15)) == (date(2027, 12, 1), date(2028, 2, 29))
    # 比較対象: 非うるう年（2027年）の同じQ1は2/28で終わる。
    assert fiscal_quarter_range(date(2027, 2, 15)) == (date(2026, 12, 1), date(2027, 2, 28))


# --- fiscal_quarter_number ----------------------------------------------------------------------


def test_fiscal_quarter_number_q1_december() -> None:
    assert fiscal_quarter_number(date(2026, 12, 15)) == 1


def test_fiscal_quarter_number_q1_january() -> None:
    assert fiscal_quarter_number(date(2027, 1, 15)) == 1


def test_fiscal_quarter_number_q2() -> None:
    assert fiscal_quarter_number(date(2027, 4, 1)) == 2


def test_fiscal_quarter_number_q3() -> None:
    assert fiscal_quarter_number(date(2026, 7, 1)) == 3


def test_fiscal_quarter_number_q4() -> None:
    assert fiscal_quarter_number(date(2026, 10, 1)) == 4


# --- fiscal_half_range ---------------------------------------------------------------------------


def test_fiscal_half_range_h1_covers_q1_and_q2() -> None:
    assert fiscal_half_range(date(2026, 12, 15)) == (date(2026, 12, 1), date(2027, 5, 31))
    assert fiscal_half_range(date(2027, 4, 1)) == (date(2026, 12, 1), date(2027, 5, 31))


def test_fiscal_half_range_h2_covers_q3_and_q4() -> None:
    assert fiscal_half_range(date(2026, 6, 1)) == (date(2026, 6, 1), date(2026, 11, 30))
    assert fiscal_half_range(date(2026, 10, 1)) == (date(2026, 6, 1), date(2026, 11, 30))


def test_fiscal_half_range_boundary_may_31_vs_june_1() -> None:
    assert fiscal_half_range(date(2027, 5, 31)) == (date(2026, 12, 1), date(2027, 5, 31))
    assert fiscal_half_range(date(2027, 6, 1)) == (date(2027, 6, 1), date(2027, 11, 30))


# --- fiscal_year_range -----------------------------------------------------------------------


def test_fiscal_year_range_from_december_start() -> None:
    assert fiscal_year_range(date(2026, 12, 1)) == (date(2026, 12, 1), date(2027, 11, 30))


def test_fiscal_year_range_from_january_still_same_fiscal_year() -> None:
    """1月時点でも、通期の範囲は前年12月始まり（年またぎのバグを検知する回帰テスト）。"""
    assert fiscal_year_range(date(2027, 1, 15)) == (date(2026, 12, 1), date(2027, 11, 30))


def test_fiscal_year_range_boundary_november_30_vs_december_1() -> None:
    assert fiscal_year_range(date(2026, 11, 30)) == (date(2025, 12, 1), date(2026, 11, 30))
    assert fiscal_year_range(date(2026, 12, 1)) == (date(2026, 12, 1), date(2027, 11, 30))
