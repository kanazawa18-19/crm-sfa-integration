"""クオーター着地予測（06_営業分析ロジック）。

Max（楽観）・Min（悲観）とExpected（見込み）は、それぞれ独立した別の判定基準で算出する
（2026-08-14、金沢さん方針変更）。3者の間に大小関係の保証（不変条件）は無く、
各シナリオの定義に従って算出した値をそのまま返す。

- Max（楽観）＝営業ステータスが「Aヨミ」または「Bヨミ」の未契約案件を全額計上した
  場合の最大値
- Expected（見込み）＝別プロパティ「確度」（A（最高）〜D（最低）、RequirementLevel.
  OPTIONAL）別の過去受注率で加重平均した標準値
- Min（悲観）＝営業ステータスが「Aヨミ」、または「口頭受注」「トライアル」の
  未契約案件のみを全額計上した最小値

（obasan-qualityレビューWARN対応、2026-08-14: 「Bヨミ以上」のような順序を含意する
表現は、Aヨミ〜Dヨミが確度（CONFIDENCE_LEVELSのような明示的な順序を持つ別プロパティ）
と同種の格付けであるかのような誤読を招くため避ける。営業ステータスの値は単なる
文字列の集合（whitelist）判定であり、順序関係は実装上定義されていない。
また、MinとMaxの両方に「Aヨミ」が含まれるのは意図的な重複であり、コピペミスではない
——Aヨミは楽観・悲観どちらのシナリオの根拠にもなり得るほど確度が高いステータス、
という業務判断による。）

Max/Minを「確度」ではなく営業ステータスの値（Xヨミ等）で判定するのは、営業ステータス
自体はRequirementLevel.REQUIREDで実データの入力率が高い一方、「確度」は任意入力で
入力率が低く、Max/Minの判定材料としては実質機能しにくかったため（2026-08-13の
ダッシュボード確認で、Min=Expected=Maxに退化する実例が見つかったことがきっかけ）。
Expectedは従来通り「確度」を使う（金沢さん確認済み。営業ステータスのXヨミ値と
「確度」プロパティは別物であり、混同しないよう注意）。

いずれも「契約済（契約確定）」の案件は3シナリオ共通で実現済みとして加算する。
「失注」「解約」は既に決着した死んだ案件であり、3シナリオいずれにも寄与させない
（案件管理DB「営業ステータス」の選択肢のうち、契約済・失注・解約を除いた
ACTIVE_STATUSESのみをpending＝今後決着しうるアクティブな案件として扱う）。
未契約（アクティブ）案件の扱いはシナリオごとに異なる：
- Max: 営業ステータスがMAX_SCENARIO_STATUSES（Aヨミ・Bヨミ）の未契約案件は
  全て受注すると仮定して加算（それ以外は加算しない）
- Expected: 未契約案件それぞれに確度別の過去受注率を乗じて加重平均
- Min: 営業ステータスがMIN_SCENARIO_STATUSES（Aヨミ・口頭受注・トライアル）の
  未契約案件のみ全額計上する（それ以外は加算しない）

旧実装ではMax ≧ Expected ≧ Minを常に成立させる不変条件として、MaxがExpectedを
下回る場合はExpectedまで引き上げ、MinがExpectedを上回る場合はExpectedまで
引き下げる後処理（キャップ）を行っていた。2026-08-14、Max/Minの判定基準が
「確度」から独立した「営業ステータスの値」に変わったことで、このキャップが
新しいMinの意図（Aヨミ・口頭受注・トライアル案件を確度に関係なく全額見せたい）と
衝突するようになった（例: Aヨミ案件の確度による加重額よりMinの全額計上の方が
大きい場合、キャップによりMin=Expectedにまで引き下げられ、意図した「全額」が
表示されなくなる）。この問題を受け、金沢さんの判断でキャップ処理自体を撤廃した
（Min > ExpectedやMax < Expectedが起こり得ることを許容する）。

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

# 06節「クオーター着地予測」Max/Minの対象（2026-08-14、営業ステータスの値ベースに変更。
# モジュールdocstring参照）。
MAX_SCENARIO_STATUSES = frozenset({"Aヨミ", "Bヨミ"})
MIN_SCENARIO_STATUSES = frozenset({"Aヨミ", "口頭受注", "トライアル"})

# obasan-qualityレビューWARN対応（2026-08-14）: MAX/MIN_SCENARIO_STATUSESはこのモジュール
# 内に文字列リテラルで直書きしているため、案件管理DB側で営業ステータスの選択肢が
# リネームされても実行時エラーにはならず、単に「該当0件」として静かに縮退してしまう
# （まさに今回の変更のきっかけとなった「確度の入力率低下でMax/Minが機能しなくなって
# いた」のと同種のサイレント劣化パターン）。せめてACTIVE_STATUSESの部分集合である
# ことだけでもモジュール読み込み時に検証し、選択肢が丸ごと削除された場合に
# 気づけるようにする（個別のtypoまでは検知できないが、無いよりはまし）。
assert MAX_SCENARIO_STATUSES <= ACTIVE_STATUSES, (
    "MAX_SCENARIO_STATUSES contains a value not in ACTIVE_STATUSES"
)
assert MIN_SCENARIO_STATUSES <= ACTIVE_STATUSES, (
    "MIN_SCENARIO_STATUSES contains a value not in ACTIVE_STATUSES"
)

# forecast_quarterが認識できる確度値（Expected算出用）。これ以外（未知の値）は
# 受注率0として扱われる。
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

    Max/Minは営業ステータスの値（MAX_SCENARIO_STATUSES/MIN_SCENARIO_STATUSES）で、
    Expectedは別プロパティ「確度」（confidence_win_rates）で判定する（両者は別物。
    モジュールdocstring参照）。confidence_win_ratesを省略した場合は
    config/analytics_thresholds.jsonの値（無ければDEFAULT_CONFIDENCE_WIN_RATES）を
    使う。未知の確度・未設定（None）の確度は受注率0として扱う（Expectedへの寄与なし）。
    未知の確度値（A/B/C/D以外）を検知した場合はloggingで警告する。

    「失注」「解約」ステータスの案件はACTIVE_STATUSESに含まれないため、いずれの
    シナリオにも計上されない（契約済でも失注・解約でもない、今後決着しうる
    アクティブな案件のみがpendingとして扱われる）。

    Max/Expected/Minは互いに独立した基準で算出され、大小関係は保証しない
    （Min > ExpectedやMax < Expectedが起こり得る。モジュールdocstring参照）。
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

    max_pending = [p for p in pending if p.status in MAX_SCENARIO_STATUSES]
    max_amount = ForecastAmount(
        initial_fee=confirmed_initial + sum(p.initial_fee for p in max_pending),
        mrr=confirmed_mrr + sum(p.monthly_fee for p in max_pending),
    )

    expected_amount = ForecastAmount(
        initial_fee=confirmed_initial
        + sum(p.initial_fee * win_rates.get(p.confidence, 0.0) for p in pending),
        mrr=confirmed_mrr
        + sum(p.monthly_fee * win_rates.get(p.confidence, 0.0) for p in pending),
    )

    min_pending = [p for p in pending if p.status in MIN_SCENARIO_STATUSES]
    min_amount = ForecastAmount(
        initial_fee=confirmed_initial + sum(p.initial_fee for p in min_pending),
        mrr=confirmed_mrr + sum(p.monthly_fee for p in min_pending),
    )

    return QuarterForecast(max=max_amount, expected=expected_amount, min=min_amount)
