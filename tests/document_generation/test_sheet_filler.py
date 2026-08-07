"""ラベル駆動セル書き込みロジックの単体テスト（フェイクLabelSheetsClientを使用）。

HttpSheetsValuesClientの実HTTP通信部分（get_values/update_value/find_sheet/keep_only_sheet）は
requests_mockで別途検証する。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.document_generation.sheet_filler import (
    HttpSheetsValuesClient,
    SheetsApiError,
    fill_cell_containing,
    fill_labeled_cells,
)

SPREADSHEET_ID = "sheet-abc123"
SHEET_NAME = "案件タブ1"
BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"


class FakeSheetsClient:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows
        self.updates: dict[str, str] = {}

    def get_values(self, spreadsheet_id: str, range_: str) -> list[list[Any]]:
        assert spreadsheet_id == SPREADSHEET_ID
        return self.rows

    def update_value(self, spreadsheet_id: str, cell: str, value: str) -> None:
        assert spreadsheet_id == SPREADSHEET_ID
        self.updates[cell] = value


# --- fill_labeled_cells ----------------------------------------------------------------------


def test_fill_labeled_cells_writes_to_next_non_empty_adjacent_cell() -> None:
    rows = [
        ["", "", "", "", "", "発行日：", "", "2026/8/7", "", ""],
        ["", "", "見積書NO：", "", "", "", "", "", "", ""],
    ]
    client = FakeSheetsClient(rows)

    fill_labeled_cells(
        client, SPREADSHEET_ID, SHEET_NAME, {"発行日：": "2026/08/07", "見積書NO：": "CN20260807ABCD"}
    )

    assert client.updates[f"'{SHEET_NAME}'!H1"] == "2026/08/07"
    # 見積書NO：の行には後続の非空セルが無いため、ラベルの右隣セル（D2）へ書き込む。
    assert client.updates[f"'{SHEET_NAME}'!D2"] == "CN20260807ABCD"


def test_fill_labeled_cells_matches_label_by_partial_string() -> None:
    rows = [["件名：見積り件", ""]]
    client = FakeSheetsClient(rows)

    fill_labeled_cells(client, SPREADSHEET_ID, SHEET_NAME, {"件名": "テスト案件"})

    assert client.updates[f"'{SHEET_NAME}'!B1"] == "テスト案件"


def test_fill_labeled_cells_logs_warning_and_does_not_raise_when_label_not_found(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeSheetsClient([["何も一致しない行"]])

    with caplog.at_level("WARNING"):
        fill_labeled_cells(client, SPREADSHEET_ID, SHEET_NAME, {"存在しないラベル": "値"})

    assert not client.updates
    assert "存在しないラベル" in caplog.text


def test_fill_labeled_cells_only_updates_first_matching_row_per_label() -> None:
    rows = [["件名：", "1件目"], ["件名：", "2件目"]]
    client = FakeSheetsClient(rows)

    fill_labeled_cells(client, SPREADSHEET_ID, SHEET_NAME, {"件名": "新しい件名"})

    assert client.updates == {f"'{SHEET_NAME}'!B1": "新しい件名"}


# --- fill_cell_containing ----------------------------------------------------------------------


def test_fill_cell_containing_overwrites_matching_cell() -> None:
    rows = [["", "〇〇　御中", ""]]
    client = FakeSheetsClient(rows)

    found = fill_cell_containing(client, SPREADSHEET_ID, SHEET_NAME, "御中", "テスト商店　御中")

    assert found is True
    assert client.updates[f"'{SHEET_NAME}'!B1"] == "テスト商店　御中"


def test_fill_cell_containing_returns_false_when_marker_not_found() -> None:
    client = FakeSheetsClient([["何も一致しない行"]])

    found = fill_cell_containing(client, SPREADSHEET_ID, SHEET_NAME, "御中", "テスト商店　御中")

    assert found is False
    assert not client.updates


# --- HttpSheetsValuesClient（実HTTP通信部分） ----------------------------------------------------


@pytest.fixture
def http_client() -> HttpSheetsValuesClient:
    return HttpSheetsValuesClient(access_token="secret-access-token")


def test_http_client_get_values_returns_values(requests_mock, http_client: HttpSheetsValuesClient) -> None:
    requests_mock.get(f"{BASE}/values/'{SHEET_NAME}'!A1:J60", json={"values": [["a", "b"]]})

    values = http_client.get_values(SPREADSHEET_ID, f"'{SHEET_NAME}'!A1:J60")

    assert values == [["a", "b"]]
    assert requests_mock.last_request.headers["Authorization"] == "Bearer secret-access-token"


def test_http_client_update_value_sends_put_request(
    requests_mock, http_client: HttpSheetsValuesClient
) -> None:
    requests_mock.put(f"{BASE}/values/'{SHEET_NAME}'!H1", json={})

    http_client.update_value(SPREADSHEET_ID, f"'{SHEET_NAME}'!H1", "2026/08/07")

    assert requests_mock.last_request.json() == {"values": [["2026/08/07"]]}


def test_http_client_find_sheet_returns_title_and_id_on_exact_match(
    requests_mock, http_client: HttpSheetsValuesClient
) -> None:
    requests_mock.get(
        BASE,
        json={
            "sheets": [
                {"properties": {"title": "既存クライアントA", "sheetId": 111}},
                {"properties": {"title": "雛形", "sheetId": 222}},
            ]
        },
    )

    assert http_client.find_sheet(SPREADSHEET_ID, exact_title="雛形") == ("雛形", 222)


def test_http_client_find_sheet_returns_none_when_no_exact_match(
    requests_mock, http_client: HttpSheetsValuesClient
) -> None:
    requests_mock.get(
        BASE,
        json={"sheets": [{"properties": {"title": "既存クライアントA", "sheetId": 111}}]},
    )

    assert http_client.find_sheet(SPREADSHEET_ID, exact_title="雛形") is None


def test_http_client_find_sheet_returns_none_when_no_sheets(
    requests_mock, http_client: HttpSheetsValuesClient
) -> None:
    requests_mock.get(BASE, json={"sheets": []})

    assert http_client.find_sheet(SPREADSHEET_ID, exact_title="雛形") is None


def test_http_client_keep_only_sheet_deletes_all_other_sheets(
    requests_mock, http_client: HttpSheetsValuesClient
) -> None:
    requests_mock.get(
        BASE,
        json={
            "sheets": [
                {"properties": {"sheetId": 111}},
                {"properties": {"sheetId": 222}},
                {"properties": {"sheetId": 333}},
            ]
        },
    )
    requests_mock.post(f"{BASE}:batchUpdate", json={})

    http_client.keep_only_sheet(SPREADSHEET_ID, sheet_id=222)

    assert requests_mock.last_request.json() == {
        "requests": [
            {"deleteSheet": {"sheetId": 111}},
            {"deleteSheet": {"sheetId": 333}},
        ]
    }


def test_http_client_keep_only_sheet_skips_batch_update_when_no_other_sheets(
    requests_mock, http_client: HttpSheetsValuesClient
) -> None:
    requests_mock.get(BASE, json={"sheets": [{"properties": {"sheetId": 222}}]})
    batch_mock = requests_mock.post(f"{BASE}:batchUpdate", json={})

    http_client.keep_only_sheet(SPREADSHEET_ID, sheet_id=222)

    assert not batch_mock.called


def test_http_client_raises_value_error_when_access_token_not_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)

    with pytest.raises(ValueError, match="GOOGLE_ACCESS_TOKEN"):
        HttpSheetsValuesClient()
