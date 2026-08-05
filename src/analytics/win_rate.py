"""段階別受注率・何回目以内の受注率・平均受注接触回数（06_営業分析ロジック）。

■ 用語の整理（仕様書の記載だけでは一意に決まらないため、以下の定義で実装している）
- 段階別受注率（stage_win_rates）: 総接触回数がN回「以上」に達した案件のうち、最終的に
  受注した割合。「N回目の接触時点に到達した案件の、その後の受注確度」を表す。
- 累積受注率（cumulative_win_rates）: 総接触回数がN回「以内」で決着（受注/失注）した
  案件のうち、受注した割合。「N回以内で見切りをつける運用にした場合の受注率」を表す
  （例：7回以内で受注率82%）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProjectOutcome:
    """過去案件データのうち、受注率算出に必要な最小項目。

    is_wonは呼び出し側で「営業ステータス == 契約済」等から算出して渡す
    （このモジュールはNotionのステータス文字列に依存しない）。
    """

    project_id: str
    total_contact_count: int
    is_won: bool


def stage_win_rates(
    projects: Sequence[ProjectOutcome], max_stage: int | None = None
) -> dict[int, float]:
    """1回目からmax_stage回目までの各段階における受注率を算出する。

    段階Nの分母は「総接触回数がN以上の案件」。max_stage省略時は実データの最大接触回数を使う。
    対象案件が1件もない案件データ（空リスト）の場合は空辞書を返す。

    win_pattern.pyと同様、本来は決着済み（受注/失注が確定した）案件のみを渡すべき。
    まだ結果が出ていない進行中の案件を is_won=False として混入させると、それらが
    「受注しなかった案件」として分母にカウントされてしまい、受注率が実態より下振れする。
    """
    if max_stage is None:
        max_stage = max((p.total_contact_count for p in projects), default=0)

    rates: dict[int, float] = {}
    for stage in range(1, max_stage + 1):
        reached = [p for p in projects if p.total_contact_count >= stage]
        if reached:
            rates[stage] = sum(1 for p in reached if p.is_won) / len(reached)
    return rates


def cumulative_win_rates(
    projects: Sequence[ProjectOutcome], max_stage: int | None = None
) -> dict[int, float]:
    """N回以内で決着した案件における累積受注率を算出する。

    stage_win_ratesと同様、本来は決着済み（受注/失注が確定した）案件のみを渡すべき。
    進行中案件が混入すると受注率が実態より下振れする点に注意。
    """
    if max_stage is None:
        max_stage = max((p.total_contact_count for p in projects), default=0)

    rates: dict[int, float] = {}
    for stage in range(1, max_stage + 1):
        within = [p for p in projects if p.total_contact_count <= stage]
        if within:
            rates[stage] = sum(1 for p in within if p.is_won) / len(within)
    return rates


def best_win_rate_threshold(cumulative_rates: Mapping[int, float]) -> int | None:
    """累積受注率の伸び（前段階からの増分）が最大となる接触回数の閾値を特定する。

    例：「7回以内で受注率82%」のように、最も受注率が伸びる閾値を全社指標として示すために使う。
    データが空の場合はNoneを返す。
    """
    if not cumulative_rates:
        return None

    stages = sorted(cumulative_rates)
    previous_rate = 0.0
    best_stage = stages[0]
    best_delta = cumulative_rates[stages[0]] - previous_rate

    for stage in stages:
        delta = cumulative_rates[stage] - previous_rate
        if delta > best_delta:
            best_delta = delta
            best_stage = stage
        previous_rate = cumulative_rates[stage]

    return best_stage


def average_won_contact_count(projects: Sequence[ProjectOutcome]) -> float | None:
    """受注済み案件（is_won=True）の総接触回数の平均値（全社平均受注接触回数）を算出する。

    受注済み案件が1件もない場合はNoneを返す（コンディション判定側で全社平均が
    未確定の状態を扱えるようにするため）。
    """
    won_counts = [p.total_contact_count for p in projects if p.is_won]
    if not won_counts:
        return None
    return sum(won_counts) / len(won_counts)
