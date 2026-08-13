"""チーム週報の生成ロジック（07_日報週報仕様「チーム週報」毎週金曜18:00配信想定）。

配信内容（仕様書07節）:
1. 今週の確定売上（初期費用）／ 今週獲得MRR（月額ストック）
2. 月次・クオーター目標に対する進捗率（%）
3. 営業パフォーマンス分析（全社平均受注接触回数との比較、勝ちパターン、
   メンバー別パフォーマンススコア）
4. クオーター着地予測（3段階: 🚀Max／🎯Expected／🛡Min）
5. コンディション🔴停滞リスク案件の一覧

レポートの生成（`build_weekly_report_data`によるデータ集計）と配信（`src.reports.dispatch`）は
分離しており、本モジュールは配信を一切行わない純粋関数のみで構成する。

■ 目標値（月次・クオーター目標）について
10_保留・要確認事項Q-05の通り、目標値の設定単位（全社／チーム／個人のいずれで設定するか）は
未確定。本モジュールは単位の解決には関与せず、`RevenueTarget`（初期費用・MRRの合算値）を
引数として外から受け取るのみとする。Q-05確定後、呼び出し側で単位に応じた合算処理を行うこと。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Mapping, Sequence

logger = logging.getLogger(__name__)

from src.analytics.condition import (
    Condition,
    ConditionThresholds,
    judge_condition,
)
from src.analytics.forecast import (
    ForecastProject,
    QuarterForecast,
    forecast_quarter,
)
from src.analytics.member_performance import (
    MemberActionRecord,
    MemberPerformance,
    MemberProjectRecord,
    compute_member_performance,
)
from src.analytics.win_pattern import ProposalRecord, WinPattern, analyze_win_patterns
from src.analytics.win_rate import ProjectOutcome, average_won_contact_count
# ACTIVE_STATUSES/CONFIRMED_STATUSESはforecast.py経由の再エクスポートではなく、
# 定義元のdb_schema.projectから直接importする（依存関係を1本化し、forecast.pyの
# import構成が変わってもここが追従不要になるようにするため）。
from src.db_schema.project import ACTIVE_STATUSES, CONFIRMED_STATUSES

# RevenueTarget/RevenueProgress、進捗率算出ロジックは日報（daily_report.py）と共通のため
# `_revenue_progress.py`へ切り出している（同モジュールdocstring参照。以前はここに実装があり
# daily_report.py側へバイト単位で複製されていた）。RevenueTarget/RevenueProgressはこのモジュール
# から`from src.reports.weekly_report import RevenueTarget`として参照している既存コード
# （`src/reports/batch.py`・テスト等）との互換性のため、ここでもそのまま公開し続ける。
from src.reports._revenue_progress import (
    RevenueProgress,
    RevenueTarget,
    _confirmed_amount_in_period,
    _format_initial_fee_target_note_line,
    _format_progress_lines,
    _revenue_progress,
)


@dataclass(frozen=True)
class WeeklyProjectRecord:
    """案件管理DBの1レコードのうち、週報の集計に必要な項目。"""

    project_id: str
    client_name: str
    assignee: str
    status: str
    confidence: str | None = None
    initial_fee: float = 0.0
    monthly_fee: float = 0.0
    contract_date: date | None = None
    total_contact_count: int = 0
    last_action_date: date | None = None
    next_action_date: date | None = None
    proposed_services: tuple[str, ...] = ()
    """販売件数の実績集計に使う（`_revenue_progress._confirmed_count_in_period`参照）。
    「1案件＝1販売」ではなく「1サービス＝1販売」（金沢さん確認済み、2026-08-13）:
    1案件に複数サービスが紐づく場合、サービス数の分だけ販売件数としてカウントする。
    """


@dataclass(frozen=True)
class StagnationRiskProject:
    """🔴停滞リスクと判定された案件1件分。"""

    client_name: str
    assignee: str
    total_contact_count: int
    last_action_date: date | None


@dataclass(frozen=True)
class WeeklyReportData:
    """チーム週報1回分の生成済みデータ。テンプレートへの埋め込み前の中間表現。"""

    week_start: date
    week_end: date
    weekly_confirmed_initial_fee: float
    weekly_confirmed_mrr: float
    monthly_progress: RevenueProgress
    quarterly_progress: RevenueProgress
    average_won_contact_count: float | None
    win_patterns: tuple[WinPattern, ...]
    member_performances: tuple[MemberPerformance, ...]
    quarter_forecast: QuarterForecast
    stagnation_risk_projects: tuple[StagnationRiskProject, ...]
    # 初期費用の目標を構造的に持たない目標ソース（事業計画スプレッドシート）を使っている場合のみ
    # `src.reports.batch._resolve_revenue_targets`が文言をセットする注記。目標未設定と混同
    # されないよう`_format_initial_fee_target_note_line`で進捗率セクション直後に表示する
    # （`_revenue_progress.py`モジュールdocstring参照）。
    initial_fee_target_note: str | None = None


def build_weekly_report_data(
    *,
    week_start: date,
    week_end: date,
    month_start: date,
    month_end: date,
    quarter_start: date,
    quarter_end: date,
    active_projects: Sequence[WeeklyProjectRecord],
    historical_outcomes: Sequence[ProjectOutcome],
    proposal_records: Sequence[ProposalRecord],
    monthly_target: RevenueTarget,
    quarter_target: RevenueTarget,
    member_actions: Sequence[MemberActionRecord] = (),
    confidence_win_rates: Mapping[str, float] | None = None,
    condition_thresholds: ConditionThresholds | None = None,
    min_win_pattern_sample_size: int = 3,
    as_of: date | None = None,
    initial_fee_target_note: str | None = None,
) -> WeeklyReportData:
    """案件管理DBの現況・過去の決着済み案件データから週報用データを組み立てる。

    - active_projects: **呼び出し側は、進行中案件＋今期（当クオーター）決着済み案件のみに
      絞り込んだ上で渡すこと。案件管理DB全件（過去の全クオーターの契約済み案件を含む）を
      そのまま渡すと、クオーター着地予測（`quarter_forecast`）が過去の契約済み金額まで
      積み上げてしまい、クオーターごとにリセットされない致命的な誤りとなる。** この前提を
      呼び出し側が満たしていない場合に備え、`quarter_start`〜`quarter_end`の範囲外の
      契約日を持つ契約済みレコードを検知した場合はloggingで警告する（防御的チェックであり、
      検知しても除外はしない。除外が必要な場合は呼び出し側で絞り込むこと）。
      今週・当月・当クオーターの確定売上／MRR集計、クオーター着地予測、
      コンディション判定（🔴停滞リスク抽出）、メンバー別パフォーマンス（クオリティ・
      スピード）にはこのリストを用いる。
    - historical_outcomes: 全社平均受注接触回数（`average_won_contact_count`）算出用の
      過去の決着済み案件データ（受注/失注が確定したもの）。active_projectsとは
      別集計軸（案件管理DB全件ではなく決着済みのみ）のため引数を分けている。
    - proposal_records: 勝ちパターン分析（`analyze_win_patterns`）用の提案実績。
    - member_actions: メンバー別パフォーマンス（`compute_member_performance`）の
      ボリューム・スピード算出に使うアクション管理DBレコード。**呼び出し側は
      週次範囲（week_start〜week_end）のアクションのみに絞り込んだ上で渡すこと**
      （active_projectsのクオーター範囲チェックと異なり、こちらは同種の防御的チェックを
      実装していないため、範囲外のレコードが混入してもloggingで検知されない）。省略時は
      空のため、`member_deadline_compliance_rates`のフォールバックにより、次回アクション
      期限判定対象の案件があるメンバーについてはスピードが確定した悪い実績（0%）ではなく
      未確定（None）として扱われる（ボリュームは引き続き全メンバー0件）。
    - initial_fee_target_note: 呼び出し側（`src.reports.batch._resolve_revenue_targets`）が、
      初期費用目標を構造的に持たない目標ソースを使ったと判定した場合にのみ渡す注記文言。
      省略時（None）は付与しない（`WeeklyReportData.initial_fee_target_note`docstring参照）。
    """
    confirmed_projects = [p for p in active_projects if p.status in CONFIRMED_STATUSES]

    out_of_quarter_confirmed = [
        p.project_id
        for p in confirmed_projects
        if p.contract_date is not None and not (quarter_start <= p.contract_date <= quarter_end)
    ]
    if out_of_quarter_confirmed:
        logger.warning(
            "build_weekly_report_data: クオーター範囲(%s〜%s)外の契約日を持つ契約済み"
            "レコードが混入しています（着地予測が過去分まで積み上がる可能性があります）: %s",
            quarter_start,
            quarter_end,
            sorted(out_of_quarter_confirmed),
        )

    weekly_confirmed = _confirmed_amount_in_period(confirmed_projects, week_start, week_end)

    monthly_progress = _revenue_progress(confirmed_projects, month_start, month_end, monthly_target)
    quarterly_progress = _revenue_progress(
        confirmed_projects, quarter_start, quarter_end, quarter_target
    )

    average_contacts = average_won_contact_count(historical_outcomes)

    win_patterns = tuple(
        analyze_win_patterns(proposal_records, min_sample_size=min_win_pattern_sample_size)
    )

    forecast_projects = [
        ForecastProject(
            project_id=p.project_id,
            confidence=p.confidence,
            status=p.status,
            initial_fee=p.initial_fee,
            monthly_fee=p.monthly_fee,
        )
        for p in active_projects
    ]
    quarter_forecast = forecast_quarter(forecast_projects, confidence_win_rates=confidence_win_rates)

    judgement_as_of = as_of or week_end
    stagnation_risk_projects = tuple(
        StagnationRiskProject(
            client_name=p.client_name,
            assignee=p.assignee,
            total_contact_count=p.total_contact_count,
            last_action_date=p.last_action_date,
        )
        for p in active_projects
        if p.status in ACTIVE_STATUSES
        and judge_condition(
            last_action_date=p.last_action_date,
            total_contact_count=p.total_contact_count,
            average_contact_count=average_contacts,
            is_won=False,
            as_of=judgement_as_of,
            thresholds=condition_thresholds,
        )
        == Condition.STAGNATION_RISK
    )

    member_projects = [
        MemberProjectRecord(
            project_id=p.project_id,
            member=p.assignee,
            status=p.status,
            next_action_date=p.next_action_date,
        )
        for p in active_projects
    ]
    member_performances = compute_member_performance(
        member_projects, member_actions, as_of=judgement_as_of
    )

    return WeeklyReportData(
        week_start=week_start,
        week_end=week_end,
        weekly_confirmed_initial_fee=weekly_confirmed.initial_fee,
        weekly_confirmed_mrr=weekly_confirmed.mrr,
        monthly_progress=monthly_progress,
        quarterly_progress=quarterly_progress,
        average_won_contact_count=average_contacts,
        win_patterns=win_patterns,
        member_performances=member_performances,
        quarter_forecast=quarter_forecast,
        stagnation_risk_projects=stagnation_risk_projects,
        initial_fee_target_note=initial_fee_target_note,
    )


# src/reports/weekly_report.py から見て、src/reports/templates/weekly_report.txt を指す。
DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "weekly_report.txt"


def _format_win_pattern_lines(patterns: Sequence[WinPattern]) -> str:
    if not patterns:
        return "（サンプル数が十分な勝ちパターンはありません）"
    lines = []
    for p in patterns:
        services = "／".join(sorted(p.services)) if p.services else "サービス未設定"
        lines.append(
            f"・{p.meeting_number}回目商談 × {services}: "
            f"受注率{p.win_rate * 100:.1f}%（サンプル数{p.sample_size}件）"
        )
    return "\n".join(lines)


def _format_member_performance_lines(performances: Sequence[MemberPerformance]) -> str:
    """メンバー別パフォーマンススコアを整形する。

    クオリティ・スピードがデータ不足でNoneの場合は「未確定」と表示し、"0%"と
    混同しないようにする（`compute_member_performance`のdocstring参照）。
    """
    if not performances:
        return "（メンバー別パフォーマンスデータはありません）"
    lines = []
    for perf in performances:
        quality = (
            f"{perf.quality_win_rate * 100:.1f}%" if perf.quality_win_rate is not None else "未確定"
        )
        speed = (
            f"{perf.speed_compliance_rate * 100:.1f}%"
            if perf.speed_compliance_rate is not None
            else "未確定"
        )
        overall = f"{perf.overall_score:.2f}" if perf.overall_score is not None else "未確定"
        lines.append(
            f"・{perf.member}: 総合スコア {overall}"
            f"（ボリューム: 接触{perf.volume_contact_count}回・相対スコア{perf.volume_score:.2f} / "
            f"クオリティ（受注率）: {quality} / "
            f"スピード（次回アクション期限遵守率）: {speed}）"
        )
    return "\n".join(lines)


def _format_stagnation_risk_lines(projects: Sequence[StagnationRiskProject]) -> str:
    if not projects:
        return "（🔴停滞リスク案件はありません）"
    lines = []
    for p in projects:
        last_action = p.last_action_date.isoformat() if p.last_action_date else "アクション履歴なし"
        lines.append(
            f"・{p.client_name}（担当: {p.assignee}）: "
            f"総接触回数{p.total_contact_count}回 / 最終アクション {last_action}"
        )
    return "\n".join(lines)


def generate_weekly_report_text(data: WeeklyReportData, *, template_path: Path | None = None) -> str:
    """`WeeklyReportData`をテキストテンプレートに埋め込み、配信用の週報テキストを生成する。"""
    path = template_path or DEFAULT_TEMPLATE_PATH
    template = path.read_text(encoding="utf-8")

    average_contacts_text = (
        f"{data.average_won_contact_count:.1f}回"
        if data.average_won_contact_count is not None
        else "未確定（受注実績なし）"
    )

    try:
        return template.format(
            week_start=data.week_start.isoformat(),
            week_end=data.week_end.isoformat(),
            weekly_confirmed_initial_fee=f"{data.weekly_confirmed_initial_fee:,.0f}",
            weekly_confirmed_mrr=f"{data.weekly_confirmed_mrr:,.0f}",
            monthly_progress_lines=_format_progress_lines("月次目標", data.monthly_progress),
            quarterly_progress_lines=_format_progress_lines("クオーター目標", data.quarterly_progress),
            initial_fee_target_note_line=_format_initial_fee_target_note_line(
                data.initial_fee_target_note
            ),
            average_won_contact_count_line=average_contacts_text,
            win_pattern_lines=_format_win_pattern_lines(data.win_patterns),
            member_performance_lines=_format_member_performance_lines(data.member_performances),
            max_initial_fee=f"{data.quarter_forecast.max.initial_fee:,.0f}",
            max_mrr=f"{data.quarter_forecast.max.mrr:,.0f}",
            expected_initial_fee=f"{data.quarter_forecast.expected.initial_fee:,.0f}",
            expected_mrr=f"{data.quarter_forecast.expected.mrr:,.0f}",
            min_initial_fee=f"{data.quarter_forecast.min.initial_fee:,.0f}",
            min_mrr=f"{data.quarter_forecast.min.mrr:,.0f}",
            stagnation_risk_lines=_format_stagnation_risk_lines(data.stagnation_risk_projects),
        )
    except KeyError as e:
        raise ValueError(
            f"テンプレートのプレースホルダ{{{e.args[0]}}}が不正です（ファイル: {path}）"
        ) from e
