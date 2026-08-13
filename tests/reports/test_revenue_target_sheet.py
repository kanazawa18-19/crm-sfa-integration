"""revenue_target_sheet.pyの単体テスト（実HTTP通信はrequests_mockでモック）。

フィクスチャの行構成は、実際の事業計画スプレッドシート（2026-08-13に金沢さんと確認した
「✳︎営業部事業計画（月額ver）」「✳︎販売計画」の実データ）と同じレイアウトを再現している
（実データそのものではなく、構造を保った最小限の再現）。
"""

from __future__ import annotations

from datetime import date

import pytest

from src.reports.revenue_target_sheet import (
    RevenueTargetSheetFormatError,
    RevenueTargetSheetPointer,
    fetch_all_targets,
    fetch_mrr_targets,
    fetch_unit_count_targets,
)

BASE = "https://sheets.googleapis.com/v4/spreadsheets/sheet-id"


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.reports.revenue_target_sheet.get_google_access_token", lambda: "test-token"
    )


def _values_url(sheet_name: str) -> str:
    return f"{BASE}/values/'{sheet_name}'!A1:AG60"


def test_fetch_mrr_targets_parses_month_header_and_amount_row(requests_mock) -> None:
    requests_mock.get(
        _values_url("MRR"),
        json={
            "values": [
                ["", "", "事業計画", "", "2025/12", "2026/01", "13期合計"],
                ["", "売上", "■予算", "", "610,000", "660,000", "1,270,000"],
            ]
        },
    )

    result = fetch_mrr_targets("sheet-id", "MRR")

    assert result == {date(2025, 12, 1): 610000.0, date(2026, 1, 1): 660000.0}


def test_fetch_mrr_targets_ignores_non_month_trailing_column(requests_mock) -> None:
    """「13期合計」等、月ラベル形式ではない列は月として扱わずスキップする。"""
    requests_mock.get(
        _values_url("MRR"),
        json={
            "values": [
                ["", "", "", "", "2025/12", "13期合計"],
                ["", "売上", "■予算", "", "610,000", "1,270,000"],
            ]
        },
    )

    result = fetch_mrr_targets("sheet-id", "MRR")

    assert list(result.keys()) == [date(2025, 12, 1)]


def test_fetch_mrr_targets_handles_blank_amount_as_zero(requests_mock) -> None:
    requests_mock.get(
        _values_url("MRR"),
        json={
            "values": [
                ["", "", "", "", "2025/12", "2026/01"],
                ["", "売上", "■予算", "", "", "660,000"],
            ]
        },
    )

    result = fetch_mrr_targets("sheet-id", "MRR")

    assert result[date(2025, 12, 1)] == 0.0
    assert result[date(2026, 1, 1)] == 660000.0


def test_fetch_mrr_targets_raises_when_header_row_missing(requests_mock) -> None:
    """「売上」「■予算」行が1行目にあり、月ラベル行が無い場合はfail-closedでValueError。"""
    requests_mock.get(
        _values_url("MRR"),
        json={"values": [["", "売上", "■予算", "", "610,000"]]},
    )

    with pytest.raises(RevenueTargetSheetFormatError):
        fetch_mrr_targets("sheet-id", "MRR")


def test_fetch_mrr_targets_raises_when_target_row_not_found(requests_mock) -> None:
    requests_mock.get(
        _values_url("MRR"),
        json={"values": [["", "", "", "", "2025/12"], ["", "その他の行", "", "", "1"]]},
    )

    with pytest.raises(RevenueTargetSheetFormatError):
        fetch_mrr_targets("sheet-id", "MRR")


def _unit_count_values_url(sheet_name: str) -> str:
    return f"{BASE}/values/'{sheet_name}'!A1:AG120"


def test_fetch_unit_count_targets_uses_latest_period_block(requests_mock) -> None:
    """11期→13期と積み上がる構成で、最新（一番下）の13期ブロックのみを採用する。"""
    requests_mock.get(
        _unit_count_values_url("販売計画"),
        json={
            "values": [
                ["11期"],
                ["販売数（計画）", "サービス", "2023年12月"],
                ["ホテマ", "ホテラボ", "2"],
                ["合計", "", "8"],
                [],
                ["13期"],
                ["販売数（計画）", "サービス", "2025年12月", "2026年01月"],
                ["ホテマ", "ホテラボ", "2", "2"],
                ["合計", "", "12", "15"],
                ["販売数（実績）", "サービス", "2025年12月", "2026年01月"],
                ["合計", "", "17", "8"],
            ]
        },
    )

    result = fetch_unit_count_targets("sheet-id", "販売計画")

    assert result == {date(2025, 12, 1): 12, date(2026, 1, 1): 15}


def test_fetch_unit_count_targets_does_not_pick_actual_total(requests_mock) -> None:
    """「販売数（実績）」以降の「合計」行（実績側）を計画側と誤認しない。"""
    requests_mock.get(
        _unit_count_values_url("販売計画"),
        json={
            "values": [
                ["13期"],
                ["販売数（計画）", "サービス", "2025年12月"],
                ["合計", "", "12"],
                ["販売数（実績）", "サービス", "2025年12月"],
                ["合計", "", "17"],
            ]
        },
    )

    result = fetch_unit_count_targets("sheet-id", "販売計画")

    assert result == {date(2025, 12, 1): 12}


def test_fetch_unit_count_targets_raises_when_no_period_label(requests_mock) -> None:
    requests_mock.get(
        _unit_count_values_url("販売計画"),
        json={"values": [["販売数（計画）", "サービス", "2025年12月"], ["合計", "", "12"]]},
    )

    with pytest.raises(RevenueTargetSheetFormatError):
        fetch_unit_count_targets("sheet-id", "販売計画")


def test_fetch_unit_count_targets_raises_when_total_row_missing(requests_mock) -> None:
    requests_mock.get(
        _unit_count_values_url("販売計画"),
        json={"values": [["13期"], ["販売数（計画）", "サービス", "2025年12月"]]},
    )

    with pytest.raises(RevenueTargetSheetFormatError):
        fetch_unit_count_targets("sheet-id", "販売計画")


@pytest.mark.parametrize("sheet_name", ["MRR/../drive", "MRR?x=1", "MRR#frag", "MRR'injected"])
def test_fetch_mrr_targets_rejects_sheet_name_with_disallowed_characters(
    requests_mock, sheet_name: str
) -> None:
    """シート名に`/`・`?`・`#`・`'`が含まれる場合、リクエストURLへそのまま埋め込まれる前に
    fail-closedで拒否すること（shirokuma-secレビュー: パス・クエリ文字列混入によるconfused
    deputy的な脆弱性のWARN指摘。finding #3）。実HTTPリクエストが送出されないことも確認する。
    """
    with pytest.raises(RevenueTargetSheetFormatError):
        fetch_mrr_targets("sheet-id", sheet_name)

    assert not requests_mock.request_history


def test_fetch_all_targets_skips_unconfigured_sheets(requests_mock) -> None:
    requests_mock.get(
        _values_url("MRR"),
        json={
            "values": [
                ["", "", "", "", "2025/12"],
                ["", "売上", "■予算", "", "610,000"],
            ]
        },
    )

    pointer = RevenueTargetSheetPointer(spreadsheet_id="sheet-id", mrr_sheet_name="MRR")
    mrr_targets, unit_count_targets = fetch_all_targets(pointer)

    assert mrr_targets == {date(2025, 12, 1): 610000.0}
    assert unit_count_targets == {}
