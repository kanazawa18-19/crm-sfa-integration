"""src/reports/batch.py（日報・週報バッチのオーケストレーション）の検証。

実際のNotion API・Slackへは一切アクセスしない（NotionDataSource/ReportNotifierを
フェイクへ差し替える）。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from src.reports.batch import (
    _month_range,
    _quarter_range,
    _revenue_target_from_env,
    _week_range,
    run_daily_report,
    run_report_batch,
    run_weekly_report,
)


class FakeDataSource:
    """`NotionDataSource`と同じインターフェース（get_projects/get_actions）を持つテスト用スタブ。"""

    def __init__(
        self, projects: list[dict[str, Any]] | None = None, actions: list[dict[str, Any]] | None = None
    ) -> None:
        self._projects = projects or []
        self._actions = actions or []

    def get_projects(self) -> list[dict[str, Any]]:
        return self._projects

    def get_actions(self) -> list[dict[str, Any]]:
        return self._actions


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_report(self, text: str) -> None:
        self.sent.append(text)


def _project(**overrides: Any) -> dict[str, Any]:
    base = {
        "notion_page_id": "proj-1",
        "案件名": "サンプルホテル",
        "営業ステータス": "アポ",
        "確度": "A",
        "初期費用": 100000,
        "月額費用": 30000,
        "担当メンバー": ["田中太郎"],
        "次回アクション日": None,
        "提案サービス": ["リピッテ"],
        "作成日時": "2026-08-05T09:00:00.000Z",
    }
    base.update(overrides)
    return base


def _action(**overrides: Any) -> dict[str, Any]:
    base = {
        "notion_page_id": "act-1",
        "商談回数・電話回数・メール回数（何回目）": "【電話】1回目",
        "アクション日": "2026-08-05",
        "案件名": ["proj-1"],
        "担当営業": "田中太郎",
    }
    base.update(overrides)
    return base


# --- 日付範囲ヘルパー ------------------------------------------------------------------------


def test_week_range_returns_monday_to_friday_of_the_containing_week() -> None:
    # 2026-08-05は水曜日
    assert _week_range(date(2026, 8, 5)) == (date(2026, 8, 3), date(2026, 8, 7))


def test_month_range_returns_first_and_last_day() -> None:
    assert _month_range(date(2026, 8, 15)) == (date(2026, 8, 1), date(2026, 8, 31))


def test_month_range_handles_december_year_rollover() -> None:
    assert _month_range(date(2026, 12, 15)) == (date(2026, 12, 1), date(2026, 12, 31))


def test_quarter_range_third_quarter() -> None:
    assert _quarter_range(date(2026, 8, 15)) == (date(2026, 7, 1), date(2026, 9, 30))


def test_quarter_range_fourth_quarter() -> None:
    assert _quarter_range(date(2026, 11, 1)) == (date(2026, 10, 1), date(2026, 12, 31))


def test_quarter_range_first_quarter() -> None:
    assert _quarter_range(date(2026, 2, 1)) == (date(2026, 1, 1), date(2026, 3, 31))


# --- _revenue_target_from_env ----------------------------------------------------------------


def test_revenue_target_from_env_defaults_to_zero_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("MONTHLY_TARGET_INITIAL_FEE", raising=False)
    monkeypatch.delenv("MONTHLY_TARGET_MRR", raising=False)

    target = _revenue_target_from_env("MONTHLY")

    assert target.initial_fee == 0.0
    assert target.mrr == 0.0


def test_revenue_target_from_env_reads_configured_values(monkeypatch) -> None:
    monkeypatch.setenv("QUARTER_TARGET_INITIAL_FEE", "1000000")
    monkeypatch.setenv("QUARTER_TARGET_MRR", "200000")

    target = _revenue_target_from_env("QUARTER")

    assert target.initial_fee == 1000000.0
    assert target.mrr == 200000.0


def test_revenue_target_from_env_falls_back_to_zero_on_invalid_value(monkeypatch) -> None:
    monkeypatch.setenv("MONTHLY_TARGET_INITIAL_FEE", "not-a-number")

    target = _revenue_target_from_env("MONTHLY")

    assert target.initial_fee == 0.0


# --- run_daily_report -------------------------------------------------------------------------


def test_run_daily_report_sends_generated_text_to_notifier() -> None:
    projects = [_project(notion_page_id="p1")]
    actions = [_action(notion_page_id="a1")]
    source = FakeDataSource(projects=projects, actions=actions)
    notifier = FakeNotifier()

    text = run_daily_report(date(2026, 8, 5), data_source=source, notifier=notifier)

    assert notifier.sent == [text]
    assert "サンプルホテル" in text
    assert "田中太郎" in text


def test_run_daily_report_skips_projects_without_status_or_created_date() -> None:
    projects = [
        _project(notion_page_id="p1", 営業ステータス=None),
        _project(notion_page_id="p2", 作成日時=None),
    ]
    source = FakeDataSource(projects=projects, actions=[])
    notifier = FakeNotifier()

    text = run_daily_report(date(2026, 8, 5), data_source=source, notifier=notifier)

    assert "本日の新規獲得案件はありません" in text


# --- run_weekly_report -------------------------------------------------------------------------


def test_run_weekly_report_sends_generated_text_to_notifier() -> None:
    projects = [_project(notion_page_id="p1")]
    actions = [_action(notion_page_id="a1")]
    source = FakeDataSource(projects=projects, actions=actions)
    notifier = FakeNotifier()

    text = run_weekly_report(date(2026, 8, 7), data_source=source, notifier=notifier)  # 金曜

    assert notifier.sent == [text]
    assert "チーム週報" in text


def test_run_weekly_report_excludes_confirmed_projects_from_other_quarters() -> None:
    """weekly_report.build_weekly_report_dataのdocstring通り、当クオーター外の契約済み案件を
    フォワード予測（confirmed_initial等）に混入させないことを確認する
    （run_weekly_report自体が正しくactive_projectsを絞り込んでいるかの回帰確認）。"""
    projects = [
        _project(
            notion_page_id="p_this_quarter",
            営業ステータス="契約",
            初期費用=500000,
            月額費用=50000,
            **{"契約日 / 予想契約日": "2026-08-05"},  # 週内・クオーター内
        ),
        _project(
            notion_page_id="p_other_quarter",
            営業ステータス="契約",
            初期費用=999999999,
            月額費用=999999999,
            **{"契約日 / 予想契約日": "2026-02-01"},  # 別クオーター（Q1）
        ),
    ]
    source = FakeDataSource(projects=projects, actions=[])
    notifier = FakeNotifier()

    text = run_weekly_report(date(2026, 8, 7), data_source=source, notifier=notifier)

    assert "999,999,999円" not in text
    assert "500,000円" in text


# --- run_report_batch --------------------------------------------------------------------------


def test_run_report_batch_sends_daily_only_on_non_friday() -> None:
    source = FakeDataSource(projects=[_project()], actions=[])
    notifier = FakeNotifier()

    result = run_report_batch(as_of=date(2026, 8, 5), data_source=source, notifier=notifier)

    assert result == {
        "date": "2026-08-05",
        "daily_report_sent": True,
        "weekly_report_sent": False,
    }
    assert len(notifier.sent) == 1


def test_run_report_batch_sends_both_reports_on_friday() -> None:
    source = FakeDataSource(projects=[_project()], actions=[])
    notifier = FakeNotifier()

    result = run_report_batch(as_of=date(2026, 8, 7), data_source=source, notifier=notifier)  # 金曜

    assert result == {
        "date": "2026-08-07",
        "daily_report_sent": True,
        "weekly_report_sent": True,
    }
    assert len(notifier.sent) == 2
