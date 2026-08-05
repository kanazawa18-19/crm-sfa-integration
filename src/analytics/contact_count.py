"""総接触回数の自動カウント・チャネル別内訳（06_営業分析ロジック）。

「接触回数は『自動メールを含む全タッチポイント』を1回としてカウントする」との定義に基づき、
対象となるアクション種別（自動メール／テレアポ／訪問商談／オンライン商談）のみを合算する。
「メール」（人力メール）はアクション管理DBのアクション種別としては存在するが、仕様書の
集計対象一覧（「自動メール」「電話（テレアポ）」「訪問商談」「オンライン商談」）に含まれて
いないため、意図的に集計対象から除外している。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

# 06_営業分析ロジック「総接触回数の自動カウント」の対象アクション種別。
# action.pyのオプション名（"テレアポ"）に合わせている（仕様書本文の「電話（テレアポ）」は表記揺れ）。
COUNTABLE_ACTION_TYPES: frozenset[str] = frozenset(
    {"自動メール", "テレアポ", "訪問商談", "オンライン商談"}
)


@dataclass(frozen=True)
class ActionRecord:
    """アクション管理DBの1レコードのうち、接触回数集計に必要な最小項目。"""

    project_id: str
    action_type: str
    action_date: date | None = None


def count_total_contacts(actions: Sequence[ActionRecord]) -> dict[str, int]:
    """案件単位で総接触回数を合算する。

    集計対象外のアクション種別（例: 人力メール）しか持たない案件はキーに現れない
    （呼び出し側で存在しない案件IDを0件として扱いたい場合は`.get(project_id, 0)`を使うこと）。
    """
    counts: dict[str, int] = {}
    for action in actions:
        if action.action_type in COUNTABLE_ACTION_TYPES:
            counts[action.project_id] = counts.get(action.project_id, 0) + 1
    return counts


def count_by_channel(actions: Sequence[ActionRecord]) -> dict[str, dict[str, int]]:
    """案件単位・アクション種別単位でチャネル別内訳を算出する（総接触回数の対象種別のみ）。"""
    breakdown: dict[str, dict[str, int]] = {}
    for action in actions:
        if action.action_type not in COUNTABLE_ACTION_TYPES:
            continue
        per_project = breakdown.setdefault(action.project_id, {})
        per_project[action.action_type] = per_project.get(action.action_type, 0) + 1
    return breakdown
