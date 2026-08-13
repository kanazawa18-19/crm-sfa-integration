"""自社の会計年度（期初12月・期末11月）を扱う共通ロジック。

この会社の決算期は**暦年（1月始まり）ではなく12月始まり**である（期初12/1・期末11/30）。
`src/reports/batch.py`の週報「当クオーター」判定・`src/api/dashboard_service.py`の
クオーター/半期/通期着地予測は、いずれも本モジュールの関数を経由して期間を算出する
（各所に暦四半期の計算式を個別に書いてしまい、会計年度の前提を見落とすバグが過去に
実際に発生したため、`FISCAL_YEAR_START_MONTH`という名前付き定数に一本化した。もし
将来この前提が変わった場合はこの定数だけを書き換えれば良い設計にしている）。

会計四半期の対応（会計年度の開始月=12月を起点に3ヶ月ずつ区切る）:
- Q1 = 12月・1月・2月
- Q2 = 3月・4月・5月
- Q3 = 6月・7月・8月
- Q4 = 9月・10月・11月
- 上半期（H1）= Q1+Q2 = 12/1〜翌5/31
- 下半期（H2）= Q3+Q4 = 6/1〜11/30
- 通期 = 12/1〜翌11/30

年またぎの注意点: as_ofが1月・2月の場合、その日付が属する会計年度は「前年12月に開始した
年度」である。例えばas_of=2027-01-15の場合、会計年度開始日は2026-12-01（2027-12-01では
ない）。この年境界の扱いを誤ると「1月の集計が翌年度のクオーターとして扱われる」バグに
つながるため、`fiscal_year_start()`に判定ロジックを集約している。
"""

from __future__ import annotations

from datetime import date, timedelta

# 会計年度の開始月。この会社は12月始まり・11月末決算（期初12月・期末11月）。
FISCAL_YEAR_START_MONTH = 12


def fiscal_year_start(as_of: date) -> date:
    """as_ofが属する会計年度の開始日（12/1）を返す。

    as_ofの月が会計年度開始月（12月）以降の場合はas_ofと同じ暦年の12/1、それより前
    （1月〜11月）の場合は前年の12/1が会計年度開始日になる。
    """
    if as_of.month >= FISCAL_YEAR_START_MONTH:
        return date(as_of.year, FISCAL_YEAR_START_MONTH, 1)
    return date(as_of.year - 1, FISCAL_YEAR_START_MONTH, 1)


def fiscal_year_range(as_of: date) -> tuple[date, date]:
    """as_ofを含む通期（会計年度）の初日・末日を返す。"""
    start = fiscal_year_start(as_of)
    end = date(start.year + 1, FISCAL_YEAR_START_MONTH, 1) - timedelta(days=1)
    return start, end


def fiscal_half_range(as_of: date) -> tuple[date, date]:
    """as_ofを含む半期（上半期=Q1+Q2 / 下半期=Q3+Q4）の初日・末日を返す。"""
    year_start = fiscal_year_start(as_of)
    # 会計年度開始から数えた経過月数（0〜11）。0〜5が上半期、6〜11が下半期。
    months_since_start = _months_between(year_start, as_of)
    if months_since_start < 6:
        start = year_start
    else:
        start = _add_months(year_start, 6)
    end = _add_months(start, 6) - timedelta(days=1)
    return start, end


def fiscal_quarter_range(as_of: date) -> tuple[date, date]:
    """as_ofを含む会計四半期（Q1=12-2月/Q2=3-5月/Q3=6-8月/Q4=9-11月）の初日・末日を返す。"""
    year_start = fiscal_year_start(as_of)
    months_since_start = _months_between(year_start, as_of)
    quarter_index = months_since_start // 3  # 0〜3
    start = _add_months(year_start, quarter_index * 3)
    end = _add_months(start, 3) - timedelta(days=1)
    return start, end


def fiscal_quarter_number(as_of: date) -> int:
    """as_ofが属する会計四半期番号（1〜4）を返す。"""
    year_start = fiscal_year_start(as_of)
    months_since_start = _months_between(year_start, as_of)
    return months_since_start // 3 + 1


def _months_between(start: date, as_of: date) -> int:
    """startからas_ofまでの経過月数を返す（startの月=0）。startはas_ofと同じ会計年度の
    開始日である前提（fiscal_year_start()で算出済みの値を渡すこと）。"""
    return (as_of.year - start.year) * 12 + (as_of.month - start.month)


def _add_months(base: date, months: int) -> date:
    total_months = (base.month - 1) + months
    year = base.year + total_months // 12
    month = total_months % 12 + 1
    return date(year, month, 1)
