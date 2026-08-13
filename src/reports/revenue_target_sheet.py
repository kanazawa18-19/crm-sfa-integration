"""事業計画スプレッドシートからの月次目標インポート（07_日報週報仕様、目標値の外部データ源）。

10_保留・要確認事項Q-05（月次・クオーター目標値の設定単位・保持先が未確定）の対応として、
金沢さんが実際に運用している事業計画スプレッドシート（人手で毎期更新される）を目標値の
唯一の情報源として直接読みに行く方式を採る。Notion側に目標値の複製を持たない
（複製すると「シートを更新したのにNotion側が古いまま」という二重管理が発生するため。
2026-08-13の会話で検討・決定）。読み込み対象は`src.reports.target_settings`で設定する
スプレッドシートID・シート名のポインタのみで、値そのものは都度この場で取得する。

対象シートは2種類、どちらも人間が日常的に編集する実運用シートであり、決め打ちの行番号・
列番号ではなく「見出しテキストの位置」を基準にパースする（行の挿入等である程度のレイアウト
変化があっても壊れにくくするため）。ただし全く別レイアウトへの変更には対応できないため、
想定した見出しが見つからない場合は`ValueError`を送出し、無言で0円/範囲外の値を返すことは
しない（目標値の誤り・消失は着地予測・進捗率の信頼性に直結するため、fail-closed）。

■ MRR目標シート（例:「✳︎営業部事業計画（月額ver）」）
B列が「売上」・C列が「■予算」の行を月別MRR目標の行として採用する（会社全体の合算値。
金沢さん確認済みの通り、初期費用目標はこのシートに存在しない）。同シート内に会計年度
ブロックは1つのみ存在する前提（複数期が積み上がる構成ではない）。

■ 販売数目標シート（例:「✳︎販売計画」）
A列に「N期」（例:「13期」）とだけ書かれた行が、期ごとのブロックの開始位置。このシートは
期が進むごとに新しいブロックが下に追記されていく構成（例: 11期ブロックの下に13期
ブロックが続く）であることを実データで確認済みのため、**最も下（＝最新）の「N期」行**を
今期のブロックとして採用する。そのブロック内で最初に現れる「合計」行（A列）を月別販売数
目標として使う。同じブロック内に「販売数（実績）」セクションにも同名の「合計」行が
存在するため、「販売数（実績）」という見出しに到達したら探索を打ち切り、それより後の
「合計」行（実績側）を計画側と誤認しないようにしている。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from src.document_generation.google_auth import get_google_access_token
from src.sync_engine.clients._http import (
    ApiError,
    DEFAULT_BACKOFF_BASE_SECONDS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    raise_for_error,
    request_with_retry,
)

_SHEETS_BASE_URL = "https://sheets.googleapis.com/v4/spreadsheets"

# 月ラベルの表記ゆれ（「2026/01」「2026年01月」等）を吸収する。
_MONTH_LABEL_PATTERN = re.compile(r"^(\d{4})[/年](\d{1,2})月?$")

_PERIOD_LABEL_PATTERN = re.compile(r"^\d+期$")

# 実データの読み取り範囲上限。事業計画シートは横方向の補足情報（担当者別内訳等）も
# 多いため、月別ラベル・合計列を確実に含む範囲を広めに確保する。
_MRR_SHEET_RANGE = "A1:AG60"
_UNIT_COUNT_SHEET_RANGE = "A1:AG120"

# MRR目標シート: 月ラベル行・金額行とも、E列（0始まりindex 4）以降に月別の値が並ぶ
# （A〜D列は見出しテキスト用の予約列）。実データで確認済み（モジュールdocstring参照）。
_MRR_MONTH_COLUMN_OFFSET = 4

# 販売数目標シート: C列（0始まりindex 2）以降に月別の値が並ぶ（A・B列は見出しテキスト用）。
_UNIT_COUNT_MONTH_COLUMN_OFFSET = 2


class RevenueTargetSheetApiError(ApiError):
    """事業計画スプレッドシート読み取り時のGoogle Sheets API呼び出し失敗。"""


class RevenueTargetSheetFormatError(ValueError):
    """想定した見出し構造がシート上に見つからなかった場合に送出する（fail-closed。
    モジュールdocstring参照）。
    """


# sheet_nameはリクエストURLへ`'{sheet_name}'!{a1_range}`の形でそのまま埋め込まれる
# （下記_get_values参照）。これらの文字を許すと、シート名を装ったパス区切り文字・クエリ文字列
# 区切り文字等の混入により、Google Sheets API上の意図しないパス・パラメータへ到達しうる
# （shirokuma-secレビュー: WARN。設定値はNotion経由で永続化され毎回のバッチ実行で再利用
# されるため、一度混入すると気づくまで繰り返し悪用されうる）。
_SHEET_NAME_DISALLOWED_CHARS = frozenset("/?#'")


def _get_values(
    spreadsheet_id: str,
    sheet_name: str,
    a1_range: str,
    *,
    access_token: str | None = None,
) -> list[list[str]]:
    if any(ch in sheet_name for ch in _SHEET_NAME_DISALLOWED_CHARS):
        raise RevenueTargetSheetFormatError(
            f"シート名に使用できない文字（/ ? # '）が含まれています: {sheet_name!r}"
        )
    token = access_token if access_token is not None else get_google_access_token()
    response = request_with_retry(
        "GET",
        f"{_SHEETS_BASE_URL}/{spreadsheet_id}/values/'{sheet_name}'!{a1_range}",
        headers={"Authorization": f"Bearer {token}"},
        params={"valueRenderOption": "FORMATTED_VALUE"},
        timeout=DEFAULT_TIMEOUT_SECONDS,
        max_retries=DEFAULT_MAX_RETRIES,
        backoff_base=DEFAULT_BACKOFF_BASE_SECONDS,
    )
    raise_for_error(response, RevenueTargetSheetApiError)
    return response.json().get("values") or []


def _cell(row: list[str], index: int) -> str:
    return row[index].strip() if index < len(row) else ""


def _parse_month_label(label: str) -> date | None:
    match = _MONTH_LABEL_PATTERN.match(label.strip())
    if not match:
        return None
    return date(int(match.group(1)), int(match.group(2)), 1)


def _parse_amount(raw: str) -> float:
    """「610,000」のようなカンマ区切り表記・空文字を数値へ変換する（空文字は0として扱う）。"""
    text = raw.strip().replace(",", "").replace("円", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _extract_month_values(
    header_row: list[str], value_row: list[str], *, column_offset: int
) -> dict[date, float]:
    result: dict[date, float] = {}
    max_len = max(len(header_row), len(value_row))
    for col in range(column_offset, max_len):
        month = _parse_month_label(_cell(header_row, col))
        if month is None:
            continue
        result[month] = _parse_amount(_cell(value_row, col))
    return result


def fetch_mrr_targets(
    spreadsheet_id: str,
    sheet_name: str,
    *,
    access_token: str | None = None,
) -> dict[date, float]:
    """MRR目標シートから月別のMRR目標額を取得する（モジュールdocstring参照）。"""
    values = _get_values(spreadsheet_id, sheet_name, _MRR_SHEET_RANGE, access_token=access_token)
    for i, row in enumerate(values):
        if _cell(row, 1) == "売上" and _cell(row, 2) == "■予算":
            if i == 0:
                raise RevenueTargetSheetFormatError(
                    "「売上」「■予算」の行が1行目にあり、月ラベル行（直上の行）が存在しません"
                )
            header_row = values[i - 1]
            monthly = _extract_month_values(
                header_row, row, column_offset=_MRR_MONTH_COLUMN_OFFSET
            )
            if not monthly:
                raise RevenueTargetSheetFormatError(
                    "「売上」「■予算」行の直上に月ラベル（YYYY/MM形式）が見つかりませんでした"
                )
            return monthly
    raise RevenueTargetSheetFormatError(
        "B列が「売上」・C列が「■予算」の行が見つかりませんでした"
        "（シート構成が変わった可能性があります）"
    )


def fetch_unit_count_targets(
    spreadsheet_id: str,
    sheet_name: str,
    *,
    access_token: str | None = None,
) -> dict[date, int]:
    """販売数目標シートから、最新（最下部）の期ブロックの月別販売数目標を取得する
    （モジュールdocstring参照）。
    """
    values = _get_values(
        spreadsheet_id, sheet_name, _UNIT_COUNT_SHEET_RANGE, access_token=access_token
    )
    period_row_indices = [
        i for i, row in enumerate(values) if _PERIOD_LABEL_PATTERN.match(_cell(row, 0))
    ]
    if not period_row_indices:
        raise RevenueTargetSheetFormatError(
            "A列が「N期」形式の行が見つかりませんでした（シート構成が変わった可能性があります）"
        )

    latest_period_row = period_row_indices[-1]
    header_row_index = latest_period_row + 1
    if header_row_index >= len(values):
        raise RevenueTargetSheetFormatError(
            "最新の期ラベル行の直下に月ラベル行が見つかりませんでした"
        )
    header_row = values[header_row_index]

    for row in values[header_row_index + 1 :]:
        first_cell = _cell(row, 0)
        if first_cell == "販売数（実績）":
            break
        if first_cell == "合計":
            monthly_floats = _extract_month_values(
                header_row, row, column_offset=_UNIT_COUNT_MONTH_COLUMN_OFFSET
            )
            if not monthly_floats:
                raise RevenueTargetSheetFormatError(
                    "「合計」行に対応する月ラベルが見つかりませんでした"
                )
            return {month: int(value) for month, value in monthly_floats.items()}

    raise RevenueTargetSheetFormatError(
        "最新の期ブロック内に「販売数（計画）」の「合計」行が見つかりませんでした"
    )


@dataclass(frozen=True)
class RevenueTargetSheetPointer:
    """目標値の情報源となるスプレッドシートへのポインタ（値そのものは保持しない。
    モジュールdocstring参照）。
    """

    spreadsheet_id: str
    mrr_sheet_name: str | None = None
    unit_count_sheet_name: str | None = None


def fetch_all_targets(
    pointer: RevenueTargetSheetPointer,
    *,
    access_token: str | None = None,
) -> tuple[dict[date, float], dict[date, int]]:
    """ポインタが指す2種類のシートから月別MRR目標・月別販売数目標をまとめて取得する。

    mrr_sheet_name／unit_count_sheet_nameが未設定の場合、対応する辞書は空のまま返す
    （どちらか一方だけ運用しているケースを許容する）。
    """
    mrr_targets: dict[date, float] = {}
    if pointer.mrr_sheet_name:
        mrr_targets = fetch_mrr_targets(
            pointer.spreadsheet_id, pointer.mrr_sheet_name, access_token=access_token
        )

    unit_count_targets: dict[date, int] = {}
    if pointer.unit_count_sheet_name:
        unit_count_targets = fetch_unit_count_targets(
            pointer.spreadsheet_id, pointer.unit_count_sheet_name, access_token=access_token
        )

    return mrr_targets, unit_count_targets
