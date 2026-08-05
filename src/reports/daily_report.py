"""チーム日報の生成ロジック（07_日報週報仕様「チーム日報」毎日19:00配信想定）。

配信内容（仕様書07節）:
1. メンバー別アクション件数サマリー（テレアポ／訪問商談／オンライン商談／メールの内訳）
2. 本日の新規獲得案件リスト（取引先・提案サービス・想定金額・担当者）
3. ステータス変更のあった案件一覧（変更前 → 変更後・確度）
4. 翌営業日の次回アクション予定一覧

レポートの生成（`build_daily_report_data`によるデータ集計）と配信（`src.reports.dispatch`）は
分離しており、本モジュールは配信を一切行わない純粋関数のみで構成する。

■ メンバー別アクション件数サマリーの集計対象について
`src.analytics.contact_count.count_by_channel`は06節「総接触回数の自動カウント」用に
{自動メール, テレアポ, 訪問商談, オンライン商談}を対象としている（人力メールを除く）。
一方、本節の日報サマリーは仕様書本文で明示的に{テレアポ, 訪問商談, オンライン商談, メール}
（人力メールを含み、自動メールを除く）を対象としており、対象アクション種別の集合が異なる
（自動送信メールは個人の活動実績ではないため日報からは除外し、逆に人力メールは営業の
日々の活動として日報に含める、という06節・07節それぞれの目的に沿った意図的な違いと解釈）。
そのためcount_by_channelをそのまま転用すると人力メールが集計から漏れてしまい、
そのままでは要件を満たせない。本モジュールでは日報専用の集計対象
（DAILY_REPORT_ACTION_TYPES）を用いて同様の集計パターンで独自に実装する。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

logger = logging.getLogger(__name__)

# 07節「メンバー別アクション件数サマリー」の対象アクション種別。
# src.analytics.contact_count.COUNTABLE_ACTION_TYPESとは対象範囲が異なる点に注意
# （モジュールdocstring参照）。
DAILY_REPORT_ACTION_TYPES: frozenset[str] = frozenset(
    {"テレアポ", "訪問商談", "オンライン商談", "メール"}
)


@dataclass(frozen=True)
class DailyActionRecord:
    """アクション管理DBの1レコードのうち、日報の集計に必要な最小項目。"""

    project_id: str
    member: str
    action_type: str
    action_date: date


@dataclass(frozen=True)
class DailyProjectRecord:
    """案件管理DBの1レコードのうち、日報の集計に必要な項目。

    previous_status/status_changed_dateは「本日ステータスが変更されたか」の判定に使う。
    案件管理DBのスナップショット自体には変更履歴が無いため、変更前ステータスと変更日は
    呼び出し側（同期エンジンの変更検知・差分ログ等）が判定した上で渡す想定。
    """

    project_id: str
    client_name: str
    assignee: str
    status: str
    created_date: date
    proposed_services: tuple[str, ...] = ()
    initial_fee: float = 0.0
    monthly_fee: float = 0.0
    confidence: str | None = None
    next_action_date: date | None = None
    previous_status: str | None = None
    status_changed_date: date | None = None


@dataclass(frozen=True)
class MemberActionSummary:
    """メンバー1人分のアクション件数サマリー。"""

    member: str
    counts_by_type: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts_by_type.values())


@dataclass(frozen=True)
class NewProjectSummary:
    """本日の新規獲得案件1件分。"""

    client_name: str
    proposed_services: tuple[str, ...]
    initial_fee: float
    monthly_fee: float
    assignee: str


@dataclass(frozen=True)
class StatusChangeSummary:
    """本日ステータスが変更された案件1件分。"""

    client_name: str
    previous_status: str
    new_status: str
    confidence: str | None


@dataclass(frozen=True)
class UpcomingActionSummary:
    """翌営業日に次回アクションが予定されている案件1件分。"""

    client_name: str
    assignee: str
    next_action_date: date


@dataclass(frozen=True)
class DailyReportData:
    """チーム日報1回分の生成済みデータ。テンプレートへの埋め込み前の中間表現。"""

    report_date: date
    next_business_day: date
    member_summaries: tuple[MemberActionSummary, ...]
    new_projects: tuple[NewProjectSummary, ...]
    status_changes: tuple[StatusChangeSummary, ...]
    upcoming_actions: tuple[UpcomingActionSummary, ...]


def next_business_day(as_of: date) -> date:
    """as_ofの翌営業日を返す（土日をスキップする簡易実装）。

    祝日カレンダーは未対応（10_保留・要確認事項に祝日考慮の要否の記載は無いため、
    暫定的に土日のみを非営業日として扱う）。
    """
    next_day = as_of + timedelta(days=1)
    while next_day.weekday() >= 5:  # 5=土曜, 6=日曜
        next_day += timedelta(days=1)
    return next_day


def _build_member_summaries(
    actions: Sequence[DailyActionRecord],
    report_date: date,
) -> tuple[MemberActionSummary, ...]:
    """メンバー別・アクション種別別に件数を集計する（report_date当日分・
    DAILY_REPORT_ACTION_TYPESのみ対象）。

    呼び出し側が当日分のみを渡す保証はないため、本関数自身が
    `action_date == report_date`のフィルタを防御的に行う。
    """
    counts: dict[str, dict[str, int]] = {}
    unknown_action_types: set[str] = set()
    for action in actions:
        if action.action_date != report_date:
            continue
        if action.action_type not in DAILY_REPORT_ACTION_TYPES:
            unknown_action_types.add(action.action_type)
            continue
        per_member = counts.setdefault(action.member, {})
        per_member[action.action_type] = per_member.get(action.action_type, 0) + 1

    if unknown_action_types:
        logger.warning(
            "_build_member_summaries: 未知のaction_typeを検知しました（集計対象外として"
            "スキップされます）: %s",
            sorted(unknown_action_types),
        )

    return tuple(
        MemberActionSummary(member=member, counts_by_type=per_member)
        for member, per_member in sorted(counts.items())
    )


def build_daily_report_data(
    *,
    report_date: date,
    actions: Sequence[DailyActionRecord],
    projects: Sequence[DailyProjectRecord],
) -> DailyReportData:
    """アクション管理DB・案件管理DBの当日分レコードから日報用データを組み立てる。

    - メンバー別アクション件数サマリー: `action_date == report_date`のアクションのみ。
    - 新規獲得案件: `created_date == report_date`の案件。
    - ステータス変更案件: `status_changed_date == report_date`の案件。
    - 翌営業日の次回アクション: `next_action_date == next_business_day(report_date)`の案件。
    """
    member_summaries = _build_member_summaries(actions, report_date)

    new_projects = tuple(
        NewProjectSummary(
            client_name=p.client_name,
            proposed_services=p.proposed_services,
            initial_fee=p.initial_fee,
            monthly_fee=p.monthly_fee,
            assignee=p.assignee,
        )
        for p in projects
        if p.created_date == report_date
    )

    status_changes = tuple(
        StatusChangeSummary(
            client_name=p.client_name,
            previous_status=p.previous_status,
            new_status=p.status,
            confidence=p.confidence,
        )
        for p in projects
        if p.status_changed_date == report_date and p.previous_status is not None
    )

    target_next_business_day = next_business_day(report_date)
    upcoming_actions = tuple(
        UpcomingActionSummary(
            client_name=p.client_name,
            assignee=p.assignee,
            next_action_date=p.next_action_date,
        )
        for p in projects
        if p.next_action_date == target_next_business_day
    )

    return DailyReportData(
        report_date=report_date,
        next_business_day=target_next_business_day,
        member_summaries=member_summaries,
        new_projects=new_projects,
        status_changes=status_changes,
        upcoming_actions=upcoming_actions,
    )


# src/reports/daily_report.py から見て、src/reports/templates/daily_report.txt を指す。
DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "daily_report.txt"


def _format_member_summary_lines(summaries: Sequence[MemberActionSummary]) -> str:
    if not summaries:
        return "（本日のアクション実績はありません）"
    lines = []
    for s in summaries:
        breakdown = "／".join(
            f"{action_type} {s.counts_by_type.get(action_type, 0)}件"
            for action_type in ("テレアポ", "訪問商談", "オンライン商談", "メール")
        )
        lines.append(f"・{s.member}: 計{s.total}件（{breakdown}）")
    return "\n".join(lines)


def _format_new_project_lines(projects: Sequence[NewProjectSummary]) -> str:
    if not projects:
        return "（本日の新規獲得案件はありません）"
    lines = []
    for p in projects:
        services = "／".join(p.proposed_services) if p.proposed_services else "未設定"
        lines.append(
            f"・{p.client_name} | 提案サービス: {services} | "
            f"想定金額: 初期費用{p.initial_fee:,.0f}円 / 月額{p.monthly_fee:,.0f}円 | "
            f"担当: {p.assignee}"
        )
    return "\n".join(lines)


def _format_status_change_lines(changes: Sequence[StatusChangeSummary]) -> str:
    if not changes:
        return "（本日ステータスが変更された案件はありません）"
    lines = []
    for c in changes:
        confidence = c.confidence or "未設定"
        lines.append(
            f"・{c.client_name}: {c.previous_status} → {c.new_status}（確度: {confidence}）"
        )
    return "\n".join(lines)


def _format_upcoming_action_lines(actions: Sequence[UpcomingActionSummary]) -> str:
    if not actions:
        return "（翌営業日に予定されている次回アクションはありません）"
    lines = []
    for a in actions:
        lines.append(f"・{a.client_name}（担当: {a.assignee}）: {a.next_action_date.isoformat()}")
    return "\n".join(lines)


def generate_daily_report_text(data: DailyReportData, *, template_path: Path | None = None) -> str:
    """`DailyReportData`をテキストテンプレートに埋め込み、配信用の日報テキストを生成する。"""
    path = template_path or DEFAULT_TEMPLATE_PATH
    template = path.read_text(encoding="utf-8")

    try:
        return template.format(
            report_date=data.report_date.isoformat(),
            next_business_day=data.next_business_day.isoformat(),
            member_summary_lines=_format_member_summary_lines(data.member_summaries),
            new_project_lines=_format_new_project_lines(data.new_projects),
            status_change_lines=_format_status_change_lines(data.status_changes),
            upcoming_action_lines=_format_upcoming_action_lines(data.upcoming_actions),
        )
    except KeyError as e:
        raise ValueError(
            f"テンプレートのプレースホルダ{{{e.args[0]}}}が不正です（ファイル: {path}）"
        ) from e
