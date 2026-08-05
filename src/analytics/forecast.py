"""クオーター着地予測（06_営業分析ロジック）。

- Max（楽観）＝S・Aランク案件が全受注した場合の最大値
- Expected（見込み）＝確度別の過去受注率で加重平均した標準値
- Min（悲観）＝契約確定およびSランクのみで算出した最小値

いずれも「契約済（契約確定）」の案件は3シナリオ共通で実現済みとして加算する。
「失注」「解約」は既に決着した死んだ案件であり、3シナリオいずれにも寄与させない
（案件管理DB「営業ステータス」の選択肢のうち、契約済・失注・解約を除いた
ACTIVE_STATUSESのみをpending＝今後決着しうるアクティブな案件として扱う）。
未契約（アクティブ）案件の扱いはシナリオごとに異なる：
- Max: 確度がS・Aの未契約案件は全て受注すると仮定して加算（B・C案件は加算しない）
- Expected: 未契約案件それぞれに確度別の過去受注率を乗じて加重平均
- Min: 確度がSの未契約案件のみ受注すると仮定して加算（A・B・C案件は加算しない）

Max ≧ Expected ≧ Min は常に成立すべき不変条件だが、素朴な算出方法のままでは
以下のように崩れるケースがある。
- Max: B・Cランクの案件が多いパイプライン構成では、Expected側の加重合計（多数の
  B・Cの積み上げ）がMax側の単純合計（S・Aのみ）を上回ることがある。
- Min: Sランクの過去受注率（確度別受注率のS）が1.0未満の場合、Sランクのみの
  パイプライン等では「Sランク全受注」を仮定するMinの単純合計が、Sランクを
  受注率で割り引くExpectedを上回ることがある。
この不変条件（Max ≧ Expected ≧ Min）を保証するため、Max算出値がExpected算出値を
下回る場合はExpected算出値まで引き上げ、Min算出値がExpected算出値を上回る場合は
Expected算出値まで引き下げる後処理を行う（下記(a)方針。Max/Min算出ロジック自体を
複雑化する(b)方針より変更が小さく、既存のMax/Minの定義「S・Aランク全受注時の
最大着地」「Sランクのみの最小着地」の意味をそのまま保てるため採用）。

初期費用（スポット売上）と月額費用（MRR・ストック売上）は分けて算出する。
確度別の過去受注率は10_保留・要確認事項Q-04の通り初期値であり、
config/analytics_thresholds.json で外出しし運用しながら調整可能にしている。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)

# src/analytics/forecast.py から見て、リポジトリルート/config/ を指す。
DEFAULT_THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "analytics_thresholds.json"
)

# 案件管理DB「営業ステータス」において契約確定を表す値。
CONFIRMED_STATUS = "契約済"

# 案件管理DB「営業ステータス」において既に決着（死亡）した案件を表す値。
# これらはMax/Expected/Minいずれのシナリオにも寄与させない。
LOST_STATUS = "失注"
CANCELLED_STATUS = "解約"

# 案件管理DB「営業ステータス」選択肢（03_プロパティ定義／src/db_schema/project.py）
# から契約済・失注・解約を除いた、今後決着しうるアクティブな状態。
ACTIVE_STATUSES = frozenset(
    {
        "初回接触",
        "提案中",
        "見積提出",
        "商談中(B)",
        "商談中(C)",
    }
)

# 06節「クオーター着地予測」Max/Minの対象ランク。
MAX_SCENARIO_RANKS = frozenset({"S", "A"})
MIN_SCENARIO_RANKS = frozenset({"S"})

# forecast_quarterが認識できる確度値。これ以外（未知の値）は受注率0として扱われる。
KNOWN_CONFIDENCE_RANKS = frozenset({"S", "A", "B", "C"})

DEFAULT_CONFIDENCE_WIN_RATES: dict[str, float] = {
    "S": 0.8,
    "A": 0.5,
    "B": 0.2,
    "C": 0.05,
}


@dataclass(frozen=True)
class ForecastProject:
    """クオーター着地予測に必要な最小項目。"""

    project_id: str
    confidence: str | None  # "S" / "A" / "B" / "C"。未設定はNone
    status: str
    initial_fee: float = 0.0
    monthly_fee: float = 0.0


@dataclass(frozen=True)
class ForecastAmount:
    """初期費用（スポット）とMRR（ストック）を分けて持つ金額。"""

    initial_fee: float
    mrr: float


@dataclass(frozen=True)
class QuarterForecast:
    """クオーター着地予測の3段階シミュレーション結果。"""

    max: ForecastAmount
    expected: ForecastAmount
    min: ForecastAmount


def load_confidence_win_rates(path: Path | None = None) -> dict[str, float]:
    """config/analytics_thresholds.jsonから確度別の過去受注率を読み込む。

    設定ファイル・キーが無い場合は仕様書記載の初期値にフォールバックする。
    """
    target_path = path or DEFAULT_THRESHOLDS_PATH
    if not target_path.exists():
        return dict(DEFAULT_CONFIDENCE_WIN_RATES)

    raw = json.loads(target_path.read_text(encoding="utf-8"))
    rates = raw.get("confidence_win_rates", {})
    return {**DEFAULT_CONFIDENCE_WIN_RATES, **rates}


def forecast_quarter(
    projects: Sequence[ForecastProject],
    *,
    confidence_win_rates: Mapping[str, float] | None = None,
) -> QuarterForecast:
    """Max（楽観）/Expected（見込み）/Min（悲観）の3段階でクオーター着地を予測する。

    confidence_win_ratesを省略した場合はconfig/analytics_thresholds.jsonの値
    （無ければDEFAULT_CONFIDENCE_WIN_RATES）を使う。未知の確度・未設定（None）の
    確度は受注率0として扱う（Expectedへの寄与なし、Max/Minの対象ランクにも含まれない）。
    未知の確度値（S/A/B/C以外）を検知した場合はloggingで警告する。

    「失注」「解約」ステータスの案件はACTIVE_STATUSESに含まれないため、いずれの
    シナリオにも計上されない（契約済でも失注・解約でもない、今後決着しうる
    アクティブな案件のみがpendingとして扱われる）。

    Max ≧ Expected ≧ Min の不変条件を保証するため、Max算出値がExpected算出値を
    下回る場合はExpected算出値まで引き上げ、Min算出値がExpected算出値を上回る場合は
    Expected算出値まで引き下げる（詳細はモジュールdocstring参照）。
    """
    win_rates = (
        dict(confidence_win_rates)
        if confidence_win_rates is not None
        else load_confidence_win_rates()
    )

    confirmed = [p for p in projects if p.status == CONFIRMED_STATUS]
    pending = [p for p in projects if p.status in ACTIVE_STATUSES]

    unknown_confidences = {
        p.confidence
        for p in pending
        if p.confidence is not None and p.confidence not in KNOWN_CONFIDENCE_RANKS
    }
    if unknown_confidences:
        logger.warning(
            "forecast_quarter: 未知の確度値を検知しました（受注率0として扱われます）: %s",
            sorted(unknown_confidences),
        )

    confirmed_initial = sum(p.initial_fee for p in confirmed)
    confirmed_mrr = sum(p.monthly_fee for p in confirmed)

    max_pending = [p for p in pending if p.confidence in MAX_SCENARIO_RANKS]
    max_initial = confirmed_initial + sum(p.initial_fee for p in max_pending)
    max_mrr = confirmed_mrr + sum(p.monthly_fee for p in max_pending)

    expected_amount = ForecastAmount(
        initial_fee=confirmed_initial
        + sum(p.initial_fee * win_rates.get(p.confidence, 0.0) for p in pending),
        mrr=confirmed_mrr
        + sum(p.monthly_fee * win_rates.get(p.confidence, 0.0) for p in pending),
    )

    # Maxが「最大値」であることを保証する後処理（モジュールdocstring参照）。
    max_amount = ForecastAmount(
        initial_fee=max(max_initial, expected_amount.initial_fee),
        mrr=max(max_mrr, expected_amount.mrr),
    )

    min_pending = [p for p in pending if p.confidence in MIN_SCENARIO_RANKS]
    min_initial = confirmed_initial + sum(p.initial_fee for p in min_pending)
    min_mrr = confirmed_mrr + sum(p.monthly_fee for p in min_pending)

    # Minが「最小値」であることを保証する後処理（モジュールdocstring参照）。
    # SランクのExpected加重率が1.0未満の場合、Sランクのみのパイプライン等では
    # 「Sランク全受注」を仮定するMinがExpectedを上回ってしまうため、同様にキャップする。
    min_amount = ForecastAmount(
        initial_fee=min(min_initial, expected_amount.initial_fee),
        mrr=min(min_mrr, expected_amount.mrr),
    )

    return QuarterForecast(max=max_amount, expected=expected_amount, min=min_amount)
