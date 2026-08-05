"""コンディション自動判定（06_営業分析ロジック）。

🟢順調：最終アクションから14日以内 かつ 総接触回数 ≦ 全社平均
🟡要フォロー：最終アクションから14日超過
🔴停滞リスク：総接触回数が全社平均の1.5倍を超えても契約に至っていない

「全社平均」は06節「平均受注接触回数」（受注済み案件の総接触回数の平均値）を指す。
本モジュールはその値を引数として受け取るのみで、算出自体は
`src.analytics.win_rate.average_won_contact_count` の責務とする。

閾値（14日・1.5倍）は10_保留・要確認事項Q-04の通り初期値であり、
config/analytics_thresholds.json で外出しし運用しながら調整可能にしている。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path

# src/analytics/condition.py から見て、リポジトリルート/config/ を指す。
DEFAULT_THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "analytics_thresholds.json"
)

DEFAULT_STALE_DAYS = 14
DEFAULT_STAGNATION_MULTIPLIER = 1.5


class Condition(str, Enum):
    """案件管理DB「コンディション判定」セレクトの選択肢。"""

    GOOD = "順調"
    NEEDS_FOLLOW_UP = "要フォロー"
    STAGNATION_RISK = "停滞リスク"


@dataclass(frozen=True)
class ConditionThresholds:
    """コンディション判定に使う閾値。"""

    stale_days: int = DEFAULT_STALE_DAYS
    stagnation_multiplier: float = DEFAULT_STAGNATION_MULTIPLIER


def load_condition_thresholds(path: Path | None = None) -> ConditionThresholds:
    """config/analytics_thresholds.jsonから閾値を読み込む。

    設定ファイルが存在しない、またはキーが無い場合は仕様書記載の初期値にフォールバックする。
    """
    target_path = path or DEFAULT_THRESHOLDS_PATH
    if not target_path.exists():
        return ConditionThresholds()

    raw = json.loads(target_path.read_text(encoding="utf-8"))
    condition_config = raw.get("condition", {})
    return ConditionThresholds(
        stale_days=condition_config.get("stale_days", DEFAULT_STALE_DAYS),
        stagnation_multiplier=condition_config.get(
            "stagnation_multiplier", DEFAULT_STAGNATION_MULTIPLIER
        ),
    )


def judge_condition(
    *,
    last_action_date: date | None,
    total_contact_count: int,
    average_contact_count: float | None,
    is_won: bool,
    as_of: date,
    thresholds: ConditionThresholds | None = None,
) -> Condition:
    """1案件のコンディションを判定する。

    3条件は文言上そのままでは網羅的ではない（例：14日以内かつ平均超〜1.5倍以内で
    未契約、のようなグレーゾーンが存在する）ため、以下の優先順位で判定する。
    1. 🔴停滞リスク（未契約 かつ 総接触回数 > 全社平均 × stagnation_multiplier）を最優先で判定
    2. 🟡要フォロー（最終アクション日が無い、または stale_days 超過）
    3. 🟢順調（総接触回数 ≦ 全社平均）
    4. 上記のいずれにも当てはまらないグレーゾーンは、フォロー漏れを防ぐ安全側の判断として
       🟡要フォローとする

    average_contact_countがNone、または0以下の場合は「受注実績がまだ無い立ち上げ期」
    等で全社平均受注接触回数が未確定であるとみなし、平均値に依存する判定
    （上記1.の🔴停滞リスク判定、および3.の🟢順調の平均比較）をスキップする。
    この場合は最終アクション日からの経過日数のみで🟢順調 / 🟡要フォローを判定する
    （stale_days以内なら🟢順調、超過なら🟡要フォローにフォールバックする）。

    is_wonはあくまで「受注済みかどうか」のみを表す。営業ステータスが失注・解約など
    既に決着した案件も is_won=False で渡されると🔴停滞リスクの判定対象に含まれて
    しまうため、呼び出し側は失注・解約など既に決着した案件をあらかじめ判定対象から
    除外した上で本関数を呼び出すこと。
    """
    th = thresholds or ConditionThresholds()
    average_is_known = average_contact_count is not None and average_contact_count > 0

    if (
        average_is_known
        and not is_won
        and total_contact_count > average_contact_count * th.stagnation_multiplier
    ):
        return Condition.STAGNATION_RISK

    days_since_last_action = (
        None if last_action_date is None else (as_of - last_action_date).days
    )
    if days_since_last_action is None or days_since_last_action > th.stale_days:
        return Condition.NEEDS_FOLLOW_UP

    if not average_is_known:
        return Condition.GOOD

    if total_contact_count <= average_contact_count:
        return Condition.GOOD

    return Condition.NEEDS_FOLLOW_UP
