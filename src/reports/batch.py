"""日報・週報バッチのオーケストレーション（07_日報週報仕様）。

Notion 6DBのうち案件管理DB・アクション履歴DBから対象データを取得し（`NotionDataSource`、
`src/api/dashboard_service.py`と同じ取得パターンを流用する）、`src/reports/daily_report.py`・
`src/reports/weekly_report.py`の純粋関数へ渡してレポートテキストを組み立て、
`src/reports/dispatch.py`のNotifierへ配信する一連の処理をまとめる。

日報は毎日、週報は週次（`_WEEKLY_REPORT_WEEKDAY`＝金曜日判定）で分岐する
（`run_report_batch()`がその分岐を担う。Vercel Cronからは1日1回この関数を呼べば良い）。

■ 実装していない項目について（`run_daily_report`・`run_weekly_report`共通）
- 勝ちパターン分析（`ProposalRecord`）: アクション履歴DBのtitle自由記述からは
  「何回目の商談で・どのサービス構成を提案したか」を確度高く復元できない
  （`src/api/action_classifier.py`のヒューリスティックはaction_typeの推定のみで
  meeting_numberは推定していない）。不正確な値で勝ちパターンを表示すると意思決定を
  誤らせるリスクの方が大きいため、本バッチでは`proposal_records=()`（空）のまま渡す
  （`analyze_win_patterns`は空リストなら「サンプル数が十分な勝ちパターンはありません」
  を返す）。
- 月次・クオーター目標値（`RevenueTarget`）: 10_保留・要確認事項Q-05（目標値の設定単位が
  全社／チーム／個人のいずれか未確定）のため、Notion側に目標値を保持するプロパティが
  存在しない。本バッチでは`MONTHLY_TARGET_INITIAL_FEE`等の環境変数から読み取り、
  未設定時は0（進捗率は`RevenueProgress`側の仕様によりNone＝目標未設定として表示される）
  とする。Q-05確定後、目標値の永続化先を用意し置き換えること。
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.analytics.contact_count import COUNTABLE_ACTION_TYPES, ActionRecord, count_total_contacts
from src.analytics.fiscal_calendar import fiscal_quarter_range
from src.analytics.member_performance import MemberActionRecord
from src.analytics.win_rate import ProjectOutcome
from src.api.action_classifier import classify_action_type
from src.api.dashboard_service import (
    ACTION_TITLE_PROP,
    NotionDataSource,
    PROP_アクション日,
    PROP_初期費用,
    PROP_契約日,
    PROP_営業ステータス,
    PROP_担当メンバー,
    PROP_担当営業,
    PROP_提案サービス,
    PROP_月額費用,
    PROP_次回アクション日,
    PROP_確度,
    PROP_案件名,
    PROP_案件名_ACTION,
    PROP_作成日時,
)
from src.db_schema.project import ACTIVE_STATUSES, CONFIRMED_STATUSES, LOST_STATUSES
from src.reports.daily_report import (
    DailyActionRecord,
    DailyProjectRecord,
    build_daily_report_data,
    generate_daily_report_text,
)
from src.reports.dispatch import ReportNotifier, WebhookSlackReportNotifier
from src.reports.weekly_report import (
    RevenueTarget,
    WeeklyProjectRecord,
    build_weekly_report_data,
    generate_weekly_report_text,
)

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

# 07_日報週報仕様「チーム週報」毎週金曜18:00配信想定（date.weekday(): 月=0 … 金=4）。
_WEEKLY_REPORT_WEEKDAY = 4

# Notionプロパティ名ではなく、案件管理DBに対応するプロパティが存在しないため
# 環境変数から読み取る月次・クオーター目標値のプレフィックス（モジュールdocstring参照）。
_TARGET_ENV_PREFIXES = ("MONTHLY", "QUARTER")


def _today_jst() -> date:
    return datetime.now(_JST).date()


def _week_range(as_of: date) -> tuple[date, date]:
    """as_ofを含む週の月曜日・金曜日を返す。"""
    monday = as_of - timedelta(days=as_of.weekday())
    friday = monday + timedelta(days=4)
    return monday, friday


def _month_range(as_of: date) -> tuple[date, date]:
    """as_ofを含む月の初日・末日を返す。"""
    start = as_of.replace(day=1)
    if start.month == 12:
        next_month_start = start.replace(year=start.year + 1, month=1)
    else:
        next_month_start = start.replace(month=start.month + 1)
    return start, next_month_start - timedelta(days=1)


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value)[:10])


def _first_relation_id(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    return None


def _revenue_target_from_env(prefix: str) -> RevenueTarget:
    def _float_env(name: str) -> float:
        raw = os.environ.get(name)
        if not raw:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            logger.warning("環境変数%sの値%rをfloatへ変換できませんでした。0として扱います", name, raw)
            return 0.0

    return RevenueTarget(
        initial_fee=_float_env(f"{prefix}_TARGET_INITIAL_FEE"),
        mrr=_float_env(f"{prefix}_TARGET_MRR"),
    )


def _build_daily_records(
    projects: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> tuple[list[DailyActionRecord], list[DailyProjectRecord]]:
    action_records = [
        DailyActionRecord(
            project_id=_first_relation_id(a.get(PROP_案件名_ACTION)) or a["notion_page_id"],
            member=a.get(PROP_担当営業) or "未設定",
            action_type=classify_action_type(a.get(ACTION_TITLE_PROP)),
            action_date=action_date,
        )
        for a in actions
        if (action_date := _parse_date(a.get(PROP_アクション日))) is not None
    ]

    project_records = [
        DailyProjectRecord(
            project_id=p["notion_page_id"],
            client_name=p.get(PROP_案件名) or "",
            assignee=(p.get(PROP_担当メンバー) or ["未設定"])[0],
            status=p.get(PROP_営業ステータス),
            created_date=created_date,
            proposed_services=tuple(p.get(PROP_提案サービス) or ()),
            initial_fee=p.get(PROP_初期費用) or 0.0,
            monthly_fee=p.get(PROP_月額費用) or 0.0,
            confidence=p.get(PROP_確度),
            next_action_date=_parse_date(p.get(PROP_次回アクション日)),
        )
        for p in projects
        if p.get(PROP_営業ステータス) is not None
        and (created_date := _parse_date(p.get(PROP_作成日時))) is not None
    ]
    return action_records, project_records


def run_daily_report(
    report_date: date,
    *,
    data_source: NotionDataSource | None = None,
    notifier: ReportNotifier | None = None,
) -> str:
    """日報を生成し配信する。生成したテキストを返す（呼び出し側でのログ・テスト用）。

    月次・クオーター目標進捗率、営業パフォーマンス分析（全社平均受注接触回数・勝ちパターン）は
    週報（`run_weekly_report`）と同じ組み立て方をする。クオーターは暦四半期ではなく会計四半期
    （`src.analytics.fiscal_calendar.fiscal_quarter_range`参照）。勝ちパターン分析の
    `proposal_records`を空のまま渡す既知の制約は週報と同じ（モジュールdocstring参照）。
    """
    source = data_source or NotionDataSource()
    projects = source.get_projects()
    actions = source.get_actions()

    action_records, project_records = _build_daily_records(projects, actions)

    month_start, month_end = _month_range(report_date)
    quarter_start, quarter_end = fiscal_quarter_range(report_date)

    weekly_style_records = _build_weekly_project_records(projects, actions)
    confirmed_projects = [r for r in weekly_style_records if r.status in CONFIRMED_STATUSES]
    historical_outcomes = [
        ProjectOutcome(
            project_id=r.project_id,
            total_contact_count=r.total_contact_count,
            is_won=r.status in CONFIRMED_STATUSES,
        )
        for r in weekly_style_records
        if r.status in CONFIRMED_STATUSES or r.status in LOST_STATUSES
    ]

    data = build_daily_report_data(
        report_date=report_date,
        actions=action_records,
        projects=project_records,
        confirmed_projects=confirmed_projects,
        historical_outcomes=historical_outcomes,
        proposal_records=(),  # weekly_report.pyと同じ理由（モジュールdocstring参照）
        monthly_target=_revenue_target_from_env("MONTHLY"),
        quarter_target=_revenue_target_from_env("QUARTER"),
        month_start=month_start,
        month_end=month_end,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
    )
    text = generate_daily_report_text(data)
    (notifier or WebhookSlackReportNotifier()).send_report(text)
    return text


def _build_weekly_project_records(
    projects: list[dict[str, Any]], actions: list[dict[str, Any]]
) -> list[WeeklyProjectRecord]:
    action_dates_by_project: dict[str, list[date]] = {}
    contact_action_records: list[ActionRecord] = []
    for a in actions:
        project_id = _first_relation_id(a.get(PROP_案件名_ACTION)) or a["notion_page_id"]
        action_date = _parse_date(a.get(PROP_アクション日))
        if action_date is None:
            continue
        action_dates_by_project.setdefault(project_id, []).append(action_date)
        action_type = classify_action_type(a.get(ACTION_TITLE_PROP))
        if action_type in COUNTABLE_ACTION_TYPES:
            contact_action_records.append(
                ActionRecord(project_id=project_id, action_type=action_type, action_date=action_date)
            )

    contact_counts = count_total_contacts(contact_action_records)

    records = []
    for p in projects:
        status = p.get(PROP_営業ステータス)
        if status is None:
            continue
        project_id = p["notion_page_id"]
        dates = action_dates_by_project.get(project_id, [])
        records.append(
            WeeklyProjectRecord(
                project_id=project_id,
                client_name=p.get(PROP_案件名) or "",
                assignee=(p.get(PROP_担当メンバー) or ["未設定"])[0],
                status=status,
                confidence=p.get(PROP_確度),
                initial_fee=p.get(PROP_初期費用) or 0.0,
                monthly_fee=p.get(PROP_月額費用) or 0.0,
                contract_date=_parse_date(p.get(PROP_契約日)),
                total_contact_count=contact_counts.get(project_id, 0),
                last_action_date=max(dates) if dates else None,
                next_action_date=_parse_date(p.get(PROP_次回アクション日)),
            )
        )
    return records


def run_weekly_report(
    week_end: date,
    *,
    data_source: NotionDataSource | None = None,
    notifier: ReportNotifier | None = None,
) -> str:
    """週報を生成し配信する。生成したテキストを返す（呼び出し側でのログ・テスト用）。

    week_endを含む週（月〜金）・月・クオーターを自動算出する。クオーターは暦四半期では
    なく、自社の会計年度（期初12月・期末11月）に基づく会計四半期
    （`src.analytics.fiscal_calendar.fiscal_quarter_range`参照）。
    """
    week_start, week_end = _week_range(week_end)
    month_start, month_end = _month_range(week_end)
    quarter_start, quarter_end = fiscal_quarter_range(week_end)

    source = data_source or NotionDataSource()
    projects = source.get_projects()
    actions = source.get_actions()

    all_weekly_records = _build_weekly_project_records(projects, actions)

    # weekly_report.build_weekly_report_dataのdocstring「呼び出し側は進行中案件＋今期
    # （当クオーター）決着済み案件のみに絞り込んだ上で渡すこと」に従い、他クオーターの
    # 契約済み案件を除外する（さもないとクオーター着地予測が過去分まで積み上がる）。
    active_projects = [
        r
        for r in all_weekly_records
        if r.status in ACTIVE_STATUSES
        or (
            r.status in CONFIRMED_STATUSES
            and r.contract_date is not None
            and quarter_start <= r.contract_date <= quarter_end
        )
    ]

    historical_outcomes = [
        ProjectOutcome(
            project_id=r.project_id,
            total_contact_count=r.total_contact_count,
            is_won=r.status in CONFIRMED_STATUSES,
        )
        for r in all_weekly_records
        if r.status in CONFIRMED_STATUSES or r.status in LOST_STATUSES
    ]

    member_actions = [
        MemberActionRecord(
            project_id=_first_relation_id(a.get(PROP_案件名_ACTION)) or a["notion_page_id"],
            member=a.get(PROP_担当営業) or "未設定",
            action_type=classify_action_type(a.get(ACTION_TITLE_PROP)),
            action_date=action_date,
        )
        for a in actions
        if (action_date := _parse_date(a.get(PROP_アクション日))) is not None
        and week_start <= action_date <= week_end
    ]

    data = build_weekly_report_data(
        week_start=week_start,
        week_end=week_end,
        month_start=month_start,
        month_end=month_end,
        quarter_start=quarter_start,
        quarter_end=quarter_end,
        active_projects=active_projects,
        historical_outcomes=historical_outcomes,
        proposal_records=(),  # モジュールdocstring参照
        monthly_target=_revenue_target_from_env("MONTHLY"),
        quarter_target=_revenue_target_from_env("QUARTER"),
        member_actions=member_actions,
        as_of=week_end,
    )
    text = generate_weekly_report_text(data)
    (notifier or WebhookSlackReportNotifier()).send_report(text)
    return text


def run_report_batch(
    *,
    as_of: date | None = None,
    data_source: NotionDataSource | None = None,
    notifier: ReportNotifier | None = None,
) -> dict[str, Any]:
    """日報を毎日、週報を週次（金曜日のみ）で配信する。Vercel Cronから1日1回呼ぶ想定。

    `data_source`/`notifier`を明示的に渡さない場合、日報・週報の両方で同一のインスタンス
    （Notion API呼び出し・Slack Webhook）を使い回す。
    """
    today = as_of or _today_jst()
    source = data_source or NotionDataSource()
    report_notifier = notifier or WebhookSlackReportNotifier()

    run_daily_report(today, data_source=source, notifier=report_notifier)

    weekly_sent = False
    if today.weekday() == _WEEKLY_REPORT_WEEKDAY:
        run_weekly_report(today, data_source=source, notifier=report_notifier)
        weekly_sent = True

    return {
        "date": today.isoformat(),
        "daily_report_sent": True,
        "weekly_report_sent": weekly_sent,
    }
