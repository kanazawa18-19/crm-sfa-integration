"""日報（`daily_report.py`）・週報（`weekly_report.py`）で共通の、月次・クオーター目標に対する
進捗率算出ロジック（07_日報週報仕様）。

元々は`weekly_report.py`にのみ実装されていたが、日報にも同じ進捗率セクションが必要になった際、
非公開関数（アンダースコア始まり）をモジュールをまたいでimportする前例が本コードベースには
無かったため、`daily_report.py`側にバイト単位で複製していた。しかし複製した状態で仕様変更
（販売件数対応・目標未追跡時の注記追加等）が入るたびに2箇所を手で同期する必要があり、
片方だけ直して同期漏れが起きるリスクが高い（実際に`batch.py`の`PROP_契約日`重複でも
同種の問題が一度発生している）。本モジュールへ切り出し、両モジュールから同じ実装をimportする
ことで解消する。

モジュール名の先頭アンダースコアは、`src/sync_engine/clients/_http.py`と同じ「パッケージ内部の
実装共有用モジュールであり、公開レポート種別（daily_report/weekly_report）そのものではない」
という意図を表す命名規約に合わせている。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Sequence

from src.analytics.forecast import ForecastAmount

if TYPE_CHECKING:
    # WeeklyProjectRecordは型ヒントのみで使用し、実行時には参照しない
    # （weekly_report.py側からも本モジュールをimportするため、循環importを避ける）。
    from src.reports.weekly_report import WeeklyProjectRecord


@dataclass(frozen=True)
class RevenueTarget:
    """月次・クオーター目標値（初期費用・MRR・販売件数）。

    Q-05が未確定であるため、集計単位（全社／チーム／個人）解決済みの合算値を
    呼び出し側から渡すこと（`weekly_report.py`モジュールdocstring参照）。

    unit_countは`src.reports.revenue_target_sheet`（事業計画スプレッドシート）由来の
    目標値のみが持つ次元で、`None`は「この目標ソースでは販売件数を追跡していない」ことを
    表す（`0`＝目標値が明示的に0件、とは意味が異なるため区別する）。環境変数由来の目標
    （`src.reports.batch._revenue_target_from_env`）は元々販売件数の概念を持たないため、常に
    `None`になる。
    """

    initial_fee: float
    mrr: float
    unit_count: int | None = None


@dataclass(frozen=True)
class RevenueProgress:
    """実績・目標・進捗率(%)。目標が0（またはunit_countがNone）の項目は進捗率をNoneとする
    （ゼロ除算回避・「目標未設定」の区別）。"""

    actual: ForecastAmount
    target: RevenueTarget
    initial_fee_progress_rate: float | None
    mrr_progress_rate: float | None
    actual_unit_count: int
    unit_count_progress_rate: float | None


def _progress_rate(actual: float, target: float) -> float | None:
    if target <= 0:
        return None
    return actual / target * 100


def _unit_count_progress_rate(actual: int, target: int | None) -> float | None:
    """target未設定（None）の場合も進捗率をNoneとする（`_progress_rate`のゼロ除算回避に加え、
    「この目標ソースでは販売件数を追跡していない」ケースを扱う。RevenueTarget.unit_count
    のdocstring参照）。"""
    if target is None or target <= 0:
        return None
    return actual / target * 100


def _confirmed_amount_in_period(
    confirmed_projects: Sequence[WeeklyProjectRecord],
    period_start: date,
    period_end: date,
) -> ForecastAmount:
    """契約日がperiod_start〜period_end（両端含む）の確定売上・MRRを合算する。"""
    in_period = [
        p
        for p in confirmed_projects
        if p.contract_date is not None and period_start <= p.contract_date <= period_end
    ]
    return ForecastAmount(
        initial_fee=sum(p.initial_fee for p in in_period),
        mrr=sum(p.monthly_fee for p in in_period),
    )


def _confirmed_count_in_period(
    confirmed_projects: Sequence[WeeklyProjectRecord],
    period_start: date,
    period_end: date,
) -> int:
    """契約日がperiod_start〜period_end（両端含む）の確定案件について、販売件数を数える
    （`_confirmed_amount_in_period`と同じ絞り込み条件だが、金額の合算ではなく件数）。

    「1案件＝1販売」ではなく「1サービス＝1販売」でカウントする（金沢さん確認済み、
    2026-08-13）: 1案件に複数サービスが紐づく場合はサービス数の分だけ販売件数に計上する
    （例: 1案件にサービスA・Bが紐づいていれば2件と数える）。`proposed_services`が空の
    案件（データ不備等）は0件として扱う（1件への切り上げはしない。実績を過大に見せない
    ため）。
    """
    return sum(
        len(p.proposed_services)
        for p in confirmed_projects
        if p.contract_date is not None and period_start <= p.contract_date <= period_end
    )


def _revenue_progress(
    confirmed_projects: Sequence[WeeklyProjectRecord],
    period_start: date,
    period_end: date,
    target: RevenueTarget,
) -> RevenueProgress:
    actual = _confirmed_amount_in_period(confirmed_projects, period_start, period_end)
    actual_unit_count = _confirmed_count_in_period(confirmed_projects, period_start, period_end)
    return RevenueProgress(
        actual=actual,
        target=target,
        initial_fee_progress_rate=_progress_rate(actual.initial_fee, target.initial_fee),
        mrr_progress_rate=_progress_rate(actual.mrr, target.mrr),
        actual_unit_count=actual_unit_count,
        unit_count_progress_rate=_unit_count_progress_rate(actual_unit_count, target.unit_count),
    )


def _format_progress_lines(label: str, progress: RevenueProgress) -> str:
    """初期費用とMRRの進捗を、Slack/Teams等の狭い画面でも読めるよう別々の行に分けて返す。

    販売件数（target.unit_countがNoneでない場合のみ）は追加の1行として付け加える。
    unit_countがNoneの目標ソース（環境変数由来等）では「0件」のゴースト行を出さない
    （RevenueTarget.unit_countのdocstring参照）。
    """

    def _rate_text(rate: float | None) -> str:
        return f"{rate:.1f}%" if rate is not None else "目標未設定"

    initial_fee_line = (
        f"{label}（初期費用）: 実績{progress.actual.initial_fee:,.0f}円 / "
        f"目標{progress.target.initial_fee:,.0f}円（進捗率 {_rate_text(progress.initial_fee_progress_rate)}）"
    )
    mrr_line = (
        f"{label}（MRR）: 実績{progress.actual.mrr:,.0f}円 / "
        f"目標{progress.target.mrr:,.0f}円（進捗率 {_rate_text(progress.mrr_progress_rate)}）"
    )
    lines = [initial_fee_line, mrr_line]
    if progress.target.unit_count is not None:
        lines.append(
            f"{label}（販売件数）: 実績{progress.actual_unit_count}件 / "
            f"目標{progress.target.unit_count}件（進捗率 {_rate_text(progress.unit_count_progress_rate)}）"
        )
    return "\n".join(lines)


def _format_initial_fee_target_note_line(note: str | None) -> str:
    """`RevenueProgress`単体からは判断できない「この目標ソースは初期費用を構造的に
    追跡していない」旨の注記（`src.reports.batch._resolve_revenue_targets`が判定してノートの
    文言ごと渡してくる）を、progress_linesの直後に差し込む1行として整形する。

    noteがNone（環境変数フォールバック等、初期費用目標を保持しうるソースを使っている場合）は
    空文字を返し、テンプレート上に余計な空行を残さない（呼び出し側テンプレートの
    `{quarterly_progress_lines}{initial_fee_target_note_line}`のように、直前の行へ改行込みで
    連結する形で埋め込む前提）。
    """
    return f"\n{note}" if note else ""
