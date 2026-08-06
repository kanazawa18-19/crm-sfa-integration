"""クオーター着地予測（06_営業分析ロジック）。

実データの確度は A（最高）/ B / C / D（最低）の4段階（Sランクは存在しない）。
仕様書のS/A/B/Cを1段階シフトしたものとして扱う（S→A、A→B、B→C、C→D）。

- Max（楽観）＝Aランク案件が全受注した場合の最大値
- Expected（見込み）＝確度別の過去受注率で加重平均した標準値
- Min（悲観）＝契約確定のみで算出した最小値（実データの確度体系には仕様書上の
  Sランクに相当するものが存在しないため、未契約案件は一切見込まない最も
  保守的な値とする）

いずれも「契約済（契約確定）」の案件は3シナリオ共通で実現済みとして加算する。
「失注」「解約」は既に決着した死んだ案件であり、3シナリオいずれにも寄与させない
（案件管理DB「営業ステータス」の選択肢のうち、契約済・失注・解約を除いた
ACTIVE_STATUSESのみをpending＝今後決着しうるアクティブな案件として扱う）。
未契約（アクティブ）案件の扱いはシナリオごとに異なる：
- Max: 確度がAの未契約案件は全て受注すると仮定して加算（B・C・D案件は加算しない）
- Expected: 未契約案件それぞれに確度別の過去受注率を乗じて加重平均
- Min: 未契約案件は一切加算しない（MIN_SCENARIO_RANKSは空集合）

Max ≧ Expected ≧ Min は常に成立すべき不変条件だが、素朴な算出方法のままでは
以下のように崩れるケースがある。
- Max: B・C・Dランクの案件が多いパイプライン構成では、Expected側の加重合計（多数の
  B・C・Dの積み上げ）がMax側の単純合計（Aのみ）を上回ることがある。
- Min: MIN_SCENARIO_RANKSが空集合のため、min_pendingの単純合計は常に0で
  min_initial（min_mrr）は契約確定分と一致し、金額・過去受注率が非負である限り
  Expected（契約確定分＋非負の加重合計）を上回ることはない。ただし将来の
  仕様変更（負の金額・MIN_SCENARIO_RANKSへのランク追加等）に備えた安全網として、
  Max側と対称的にキャップ処理は残している。
この不変条件（Max ≧ Expected ≧ Min）を保証するため、Max算出値がExpected算出値を
下回る場合はExpected算出値まで引き上げ、Min算出値がExpected算出値を上回る場合は
Expected算出値まで引き下げる後処理を行う（下記(a)方針。Max/Min算出ロジック自体を
複雑化する(b)方針より変更が小さく、既存のMax/Minの定義「Aランク全受注時の
最大着地」「契約確定のみの最小着地」の意味をそのまま保てるため採用）。

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

from src.db_schema.project import ACTIVE_STATUSES, CONFIRMED_STATUSES

logger = logging.getLogger(__name__)

# src/analytics/forecast.py から見て、リポジトリルート/config/ を指す。
DEFAULT_THRESHOLDS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "analytics_thresholds.json"
)

# 06節「クオーター着地予測」Max/Minの対象ランク。
MAX_SCENARIO_RANKS = frozenset({"A"})
# 実データにはAより上のランクが存在しないため、仕様書のSランク相当の対象は
# 空集合（＝未契約案件は一切見込まない、最も保守的な悲観シナリオ）。
MIN_SCENARIO_RANKS: frozenset[str] = frozenset()

# forecast_quarterが認識できる確度値。これ以外（未知の値）は受注率0として扱われる。
KNOWN_CONFIDENCE_RANKS = frozenset({"A", "B", "C", "D"})

DEFAULT_CONFIDENCE_WIN_RATES: dict[str, float] = {
    "A": 0.8,
    "B": 0.5,
    "C": 0.2,
    "D": 0.05,
}


@dataclass(frozen=True)
class ForecastProject:
    """クオーター着地予測に必要な最小項目。"""

    project_id: str
    confidence: str | None  # "A" / "B" / "C" / "D"。未設定はNone
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
    未知の確度値（A/B/C/D以外）を検知した場合はloggingで警告する。

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

    confirmed = [p for p in projects if p.status in CONFIRMED_STATUSES]
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

    # MIN_SCENARIO_RANKSは空集合のため、min_pendingは常に空リストになり、
    # min_initial（min_mrr）は契約確定分のみと一致する（モジュールdocstring参照）。
    min_pending = [p for p in pending if p.confidence in MIN_SCENARIO_RANKS]
    min_initial = confirmed_initial + sum(p.initial_fee for p in min_pending)
    min_mrr = confirmed_mrr + sum(p.monthly_fee for p in min_pending)

    # Minが「最小値」であることを保証する後処理（モジュールdocstring参照）。
    # 通常は契約確定分のみのmin_initial/min_mrrがExpectedを上回ることはないが、
    # Max側と対称的に安全網としてキャップ処理を残す。
    min_amount = ForecastAmount(
        initial_fee=min(min_initial, expected_amount.initial_fee),
        mrr=min(min_mrr, expected_amount.mrr),
    )

    return QuarterForecast(max=max_amount, expected=expected_amount, min=min_amount)
