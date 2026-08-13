"""src/reports/batch.py（日報・週報バッチのオーケストレーション）の検証。

実際のNotion API・Slackへは一切アクセスしない（NotionDataSource/ReportNotifierを
フェイクへ差し替える）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from src.analytics.fiscal_calendar import fiscal_quarter_range
from src.reports import batch
from src.reports.batch import (
    _month_range,
    _resolve_revenue_targets,
    _revenue_target_from_env,
    _target_from_sheet_values,
    _week_range,
    run_daily_report,
    run_report_batch,
    run_weekly_report,
)
from src.reports.revenue_target_sheet import RevenueTargetSheetFormatError, RevenueTargetSheetPointer
from src.reports.revenue_target_settings import RevenueTargetSettingsRecord
from src.sync_engine.clients._http import ApiError


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


# クオーターの日付範囲自体（会計四半期）の詳細な境界値検証は
# tests/analytics/test_fiscal_calendar.py 側で行う。ここではrun_weekly_reportが
# fiscal_quarter_range()を正しく呼び出していることの回帰確認のみ行う（暦四半期の
# 1-3月/4-6月/7-9月/10-12月ではなく、会計四半期＝期初12月・期末11月であること）。


def test_run_weekly_report_uses_fiscal_quarter_not_calendar_quarter() -> None:
    """2026-08-15は暦四半期なら7-9月だが、会計四半期（期初12月）ではQ3（6-8月）になる。
    暦四半期の6-8月境界と会計四半期の境界は一致しないため、9月契約分が誤って
    「当クオーター」に混入しないことを確認する（暦四半期のままだと9/30まで含んでしまう）。"""
    quarter_start, quarter_end = fiscal_quarter_range(date(2026, 8, 21))  # 金曜
    assert (quarter_start, quarter_end) == (date(2026, 6, 1), date(2026, 8, 31))

    projects = [
        _project(
            notion_page_id="p_this_quarter",
            営業ステータス="契約",
            初期費用=500000,
            月額費用=50000,
            **{"契約日 / 予想契約日": "2026-08-05"},  # 会計Q3内
        ),
        _project(
            notion_page_id="p_next_month_calendar_quarter_but_next_fiscal_quarter",
            営業ステータス="契約",
            初期費用=999999999,
            月額費用=999999999,
            **{"契約日 / 予想契約日": "2026-09-01"},  # 暦四半期なら同じ7-9月だが会計Q4
        ),
    ]
    source = FakeDataSource(projects=projects, actions=[])
    notifier = FakeNotifier()

    text = run_weekly_report(date(2026, 8, 21), data_source=source, notifier=notifier)

    assert "999,999,999円" not in text
    assert "500,000円" in text


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


# --- _target_from_sheet_values ------------------------------------------------------------------


def test_target_from_sheet_values_sums_matching_months_and_zeroes_initial_fee() -> None:
    mrr_targets = {
        date(2026, 6, 1): 1_000_000.0,
        date(2026, 7, 1): 1_100_000.0,
        date(2026, 8, 1): 1_200_000.0,
        date(2026, 9, 1): 999_999.0,  # クオーター外なので合算されないこと
    }
    unit_count_targets = {
        date(2026, 6, 1): 10,
        date(2026, 7, 1): 11,
        date(2026, 8, 1): 12,
        date(2026, 9, 1): 999,
    }

    target = _target_from_sheet_values(
        date(2026, 6, 1), date(2026, 8, 31), mrr_targets, unit_count_targets
    )

    assert target.initial_fee == 0.0  # 事業計画シートに存在しない項目（モジュールdocstring参照）
    assert target.mrr == 3_300_000.0
    assert target.unit_count == 33


def test_target_from_sheet_values_unit_count_is_none_when_unit_count_targets_empty() -> None:
    """販売数目標シートが未設定（unit_count_targetsが空辞書）の場合、unit_count=0ではなく
    None（この目標ソースでは追跡していない）とする（RevenueTarget.unit_countのdocstring参照）。"""
    target = _target_from_sheet_values(
        date(2026, 8, 1), date(2026, 8, 31), {date(2026, 8, 1): 500_000.0}, {}
    )

    assert target.unit_count is None


# --- _resolve_revenue_targets ------------------------------------------------------------------


class FakeSettingsStore:
    def __init__(self, record: RevenueTargetSettingsRecord | None) -> None:
        self._record = record

    def get(self) -> RevenueTargetSettingsRecord | None:
        return self._record


_MONTH_START = date(2026, 8, 1)
_MONTH_END = date(2026, 8, 31)
_QUARTER_START = date(2026, 6, 1)
_QUARTER_END = date(2026, 8, 31)


@pytest.fixture(autouse=True)
def _reset_target_sheet_cache():
    """`_resolve_revenue_targets`のTTLキャッシュがテスト間で汚染されないようにする。"""
    batch.reset_target_sheet_cache()
    yield
    batch.reset_target_sheet_cache()


def test_resolve_revenue_targets_falls_back_to_env_when_no_pointer_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(batch, "build_revenue_target_settings_store", lambda: None)
    monkeypatch.setenv("MONTHLY_TARGET_INITIAL_FEE", "1000000")
    monkeypatch.setenv("MONTHLY_TARGET_MRR", "200000")

    monthly_target, _, note = _resolve_revenue_targets(
        _MONTH_START, _MONTH_END, _QUARTER_START, _QUARTER_END
    )

    assert monthly_target.initial_fee == 1000000.0
    assert monthly_target.mrr == 200000.0
    assert monthly_target.unit_count is None
    assert note is None  # 環境変数由来は初期費用目標を保持できるため注記不要


def test_resolve_revenue_targets_falls_back_to_env_when_settings_record_not_saved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定ストアは構成されている（環境変数あり）が、まだレコードを保存していない
    （store.get()がNone）場合も、未構成と同じく環境変数へフォールバックすること。"""
    monkeypatch.setattr(batch, "build_revenue_target_settings_store", lambda: FakeSettingsStore(None))
    monkeypatch.delenv("MONTHLY_TARGET_MRR", raising=False)

    monthly_target, _, note = _resolve_revenue_targets(
        _MONTH_START, _MONTH_END, _QUARTER_START, _QUARTER_END
    )

    assert monthly_target.mrr == 0.0
    assert note is None


def test_resolve_revenue_targets_uses_sheet_values_when_pointer_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pointer = RevenueTargetSheetPointer(
        spreadsheet_id="sheet-abc", mrr_sheet_name="MRRシート", unit_count_sheet_name="販売数シート"
    )
    record = RevenueTargetSettingsRecord(pointer=pointer, updated_at=datetime(2026, 8, 1))
    monkeypatch.setattr(batch, "build_revenue_target_settings_store", lambda: FakeSettingsStore(record))

    mrr_targets = {
        date(2026, 6, 1): 1_000_000.0,
        date(2026, 7, 1): 1_100_000.0,
        date(2026, 8, 1): 1_200_000.0,
    }
    unit_count_targets = {date(2026, 6, 1): 10, date(2026, 7, 1): 11, date(2026, 8, 1): 12}
    monkeypatch.setattr(batch, "fetch_all_targets", lambda p, **kw: (mrr_targets, unit_count_targets))

    monthly_target, quarter_target, note = _resolve_revenue_targets(
        _MONTH_START, _MONTH_END, _QUARTER_START, _QUARTER_END
    )

    assert monthly_target.initial_fee == 0.0
    assert monthly_target.mrr == 1_200_000.0
    assert monthly_target.unit_count == 12
    assert quarter_target.mrr == 3_300_000.0
    assert quarter_target.unit_count == 33
    # 事業計画スプレッドシート由来の場合のみ、初期費用が構造的に未追跡である旨の注記を返す
    # （目標「未設定」との混同を避けるため。finding #4）。
    assert note is not None
    assert "初期費用" in note


def test_resolve_revenue_targets_falls_back_to_env_on_sheet_format_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """シートの見出し構造が想定外（RevenueTargetSheetFormatError）でも、日報・週報の
    生成自体は止めず環境変数へフォールバックすること（モジュールdocstring参照）。"""
    pointer = RevenueTargetSheetPointer(spreadsheet_id="sheet-broken", mrr_sheet_name="MRRシート")
    record = RevenueTargetSettingsRecord(pointer=pointer, updated_at=datetime(2026, 8, 1))
    monkeypatch.setattr(batch, "build_revenue_target_settings_store", lambda: FakeSettingsStore(record))

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise RevenueTargetSheetFormatError("見出しが見つかりませんでした")

    monkeypatch.setattr(batch, "fetch_all_targets", _raise)
    monkeypatch.setenv("MONTHLY_TARGET_MRR", "300000")

    with caplog.at_level("WARNING"):
        monthly_target, _, note = _resolve_revenue_targets(
            _MONTH_START, _MONTH_END, _QUARTER_START, _QUARTER_END
        )

    assert monthly_target.mrr == 300000.0
    assert any("フォールバック" in r.getMessage() for r in caplog.records)
    assert note is None


def test_resolve_revenue_targets_falls_back_to_env_on_api_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Google Sheets API呼び出し自体の失敗（ApiError）でも同様にフォールバックすること
    （ネットワーク障害・認証切れ等で日報・週報配信が止まらないようにする）。"""
    pointer = RevenueTargetSheetPointer(spreadsheet_id="sheet-unreachable", mrr_sheet_name="MRRシート")
    record = RevenueTargetSettingsRecord(pointer=pointer, updated_at=datetime(2026, 8, 1))
    monkeypatch.setattr(batch, "build_revenue_target_settings_store", lambda: FakeSettingsStore(record))

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise ApiError(503, "service unavailable")

    monkeypatch.setattr(batch, "fetch_all_targets", _raise)
    monkeypatch.setenv("MONTHLY_TARGET_MRR", "400000")

    with caplog.at_level("WARNING"):
        monthly_target, _, note = _resolve_revenue_targets(
            _MONTH_START, _MONTH_END, _QUARTER_START, _QUARTER_END
        )

    assert monthly_target.mrr == 400000.0
    assert note is None


def test_resolve_revenue_targets_falls_back_to_env_on_missing_google_credentials(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`get_google_access_token()`（`src.document_generation.google_auth`）はGoogle認証情報が
    未設定の場合`ValueError`を送出するが、HTTPレスポンスが返る前段階の失敗のため`ApiError`の
    サブクラスにはならない。この例外がバッチ全体をクラッシュさせず、環境変数へフォールバック
    できることを確認する（BLOCKER: finding #1。`src.api.app.save_revenue_target_sheet_settings`が
    既に同じ例外タプルでこの問題に対処済みで、batch.py側は漏れていた）。"""
    pointer = RevenueTargetSheetPointer(spreadsheet_id="sheet-no-credentials", mrr_sheet_name="MRRシート")
    record = RevenueTargetSettingsRecord(pointer=pointer, updated_at=datetime(2026, 8, 1))
    monkeypatch.setattr(batch, "build_revenue_target_settings_store", lambda: FakeSettingsStore(record))

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("GOOGLE_SERVICE_ACCOUNT_JSON等のGoogle認証情報が設定されていません")

    monkeypatch.setattr(batch, "fetch_all_targets", _raise)
    monkeypatch.setenv("MONTHLY_TARGET_MRR", "500000")

    with caplog.at_level("WARNING"):
        monthly_target, _, note = _resolve_revenue_targets(
            _MONTH_START, _MONTH_END, _QUARTER_START, _QUARTER_END
        )

    assert monthly_target.mrr == 500000.0
    assert note is None
    assert any("フォールバック" in r.getMessage() for r in caplog.records)


def test_resolve_revenue_targets_falls_back_to_env_on_google_token_refresh_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`get_google_access_token()`はトークンリフレッシュ失敗時に`RuntimeError`を送出しうる
    （認証情報のローテーション中・サービスアカウント無効化等）。こちらも同様にフォールバック
    すること（finding #1）。"""
    pointer = RevenueTargetSheetPointer(spreadsheet_id="sheet-refresh-failed", mrr_sheet_name="MRRシート")
    record = RevenueTargetSettingsRecord(pointer=pointer, updated_at=datetime(2026, 8, 1))
    monkeypatch.setattr(batch, "build_revenue_target_settings_store", lambda: FakeSettingsStore(record))

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Googleサービスアカウントのトークンリフレッシュに失敗しました")

    monkeypatch.setattr(batch, "fetch_all_targets", _raise)
    monkeypatch.setenv("MONTHLY_TARGET_MRR", "600000")

    with caplog.at_level("WARNING"):
        monthly_target, _, note = _resolve_revenue_targets(
            _MONTH_START, _MONTH_END, _QUARTER_START, _QUARTER_END
        )

    assert monthly_target.mrr == 600000.0
    assert note is None
    assert any("フォールバック" in r.getMessage() for r in caplog.records)


def test_run_daily_report_falls_back_to_env_when_sheet_fetch_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_daily_report全体を通しても、シート読み取り失敗時に例外を伝播させず、環境変数の
    目標値でレポート生成を継続できること。"""
    pointer = RevenueTargetSheetPointer(spreadsheet_id="sheet-broken", mrr_sheet_name="MRRシート")
    record = RevenueTargetSettingsRecord(pointer=pointer, updated_at=datetime(2026, 8, 1))
    monkeypatch.setattr(batch, "build_revenue_target_settings_store", lambda: FakeSettingsStore(record))

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise RevenueTargetSheetFormatError("見出しが見つかりませんでした")

    monkeypatch.setattr(batch, "fetch_all_targets", _raise)
    monkeypatch.setenv("MONTHLY_TARGET_MRR", "500000")

    text = run_daily_report(date(2026, 8, 5), data_source=FakeDataSource(), notifier=FakeNotifier())

    assert "500,000円" in text


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


def test_run_daily_report_includes_target_progress_and_performance_sections() -> None:
    text = run_daily_report(date(2026, 8, 5), data_source=FakeDataSource(), notifier=FakeNotifier())

    assert "月次・クオーター目標に対する進捗率" in text
    assert "営業パフォーマンス分析" in text


def test_run_daily_report_uses_fiscal_quarter_not_calendar_quarter_for_progress() -> None:
    """run_weekly_reportと同じfiscal_quarter_range()を使っていることの回帰確認
    （暦四半期の7-9月ではなく会計四半期のQ3=6-8月であること）。"""
    # 作成日時をreport_dateからずらし、「本日の新規獲得案件」セクションへ金額が
    # 混入して進捗率セクションの検証と紛れないようにする。
    projects = [
        _project(
            notion_page_id="p_this_quarter",
            営業ステータス="契約",
            初期費用=500000,
            月額費用=50000,
            作成日時="2026-07-01T09:00:00.000Z",
            **{"契約日 / 予想契約日": "2026-08-05"},  # 会計Q3内
        ),
        _project(
            notion_page_id="p_next_fiscal_quarter",
            営業ステータス="契約",
            初期費用=999999999,
            月額費用=999999999,
            作成日時="2026-07-01T09:00:00.000Z",
            **{"契約日 / 予想契約日": "2026-09-01"},  # 暦四半期なら同じ7-9月だが会計Q4
        ),
    ]
    source = FakeDataSource(projects=projects, actions=[])
    notifier = FakeNotifier()

    text = run_daily_report(date(2026, 8, 5), data_source=source, notifier=notifier)

    assert "999,999,999円" not in text
    assert "500,000円" in text


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
