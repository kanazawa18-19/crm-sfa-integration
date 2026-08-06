"""HttpSpreadsheetClientの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

import pytest

from src.sync_engine.clients.spreadsheet_client import (
    HttpSpreadsheetClient,
    SpreadsheetApiError,
    column_letter,
)

SPREADSHEET_ID = "sheet-abc123"
BASE = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}"
SHEET = "取引先マスター"


@pytest.fixture
def client() -> HttpSpreadsheetClient:
    return HttpSpreadsheetClient(SPREADSHEET_ID, access_token="secret-access-token")


# --- 認証情報未設定時のエラー -------------------------------------------------------------------


def test_raises_value_error_when_spreadsheet_id_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPREADSHEET_ID", raising=False)

    with pytest.raises(ValueError, match="SPREADSHEET_ID"):
        HttpSpreadsheetClient(access_token="secret-access-token")


def test_raises_value_error_when_access_token_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_ACCESS_TOKEN", raising=False)

    with pytest.raises(ValueError, match="GOOGLE_ACCESS_TOKEN"):
        HttpSpreadsheetClient(SPREADSHEET_ID)


# --- get_row ---------------------------------------------------------------------------


def test_get_row_returns_dict_keyed_by_header(requests_mock, client: HttpSpreadsheetClient) -> None:
    requests_mock.get(
        f"{BASE}/values:batchGet",
        json={
            "valueRanges": [
                {"range": f"'{SHEET}'!1:1", "values": [["取引先ID", "取引先名"]]},
                {"range": f"'{SHEET}'!5:5", "values": [["CLI-005", "テスト商店"]]},
            ]
        },
    )

    row = client.get_row(SHEET, 5)

    assert row == {"取引先ID": "CLI-005", "取引先名": "テスト商店"}


def test_get_row_returns_none_when_row_has_no_values(
    requests_mock, client: HttpSpreadsheetClient
) -> None:
    requests_mock.get(
        f"{BASE}/values:batchGet",
        json={
            "valueRanges": [
                {"range": f"'{SHEET}'!1:1", "values": [["取引先ID", "取引先名"]]},
                {"range": f"'{SHEET}'!99:99"},
            ]
        },
    )

    assert client.get_row(SHEET, 99) is None


def test_get_row_sends_bearer_token(requests_mock, client: HttpSpreadsheetClient) -> None:
    requests_mock.get(f"{BASE}/values:batchGet", json={"valueRanges": []})

    client.get_row(SHEET, 1)

    assert requests_mock.last_request.headers["Authorization"] == "Bearer secret-access-token"


def test_get_row_requests_unformatted_value_render_option(
    requests_mock, client: HttpSpreadsheetClient
) -> None:
    """BLOCKER対応: 数値が"500,000"のようなカンマ区切り文字列で返らないよう、
    values:batchGetにvalueRenderOption=UNFORMATTED_VALUEを指定していることを検証する。
    """
    requests_mock.get(f"{BASE}/values:batchGet", json={"valueRanges": []})

    client.get_row(SHEET, 1)

    sent_qs = requests_mock.last_request.qs
    assert sent_qs["valuerenderoption"] == ["unformatted_value"]


def test_get_row_raises_spreadsheet_api_error_on_5xx(
    requests_mock, client: HttpSpreadsheetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(f"{BASE}/values:batchGet", status_code=500, json={"error": "boom"})

    with pytest.raises(SpreadsheetApiError):
        client.get_row(SHEET, 1)


# --- append_row --------------------------------------------------------------------------


def test_append_row_sends_values_in_header_order_and_returns_row_number(
    requests_mock, client: HttpSpreadsheetClient
) -> None:
    requests_mock.get(
        f"{BASE}/values/'{SHEET}'!1:1",
        json={"values": [["取引先ID", "取引先名", "顧客種別"]]},
    )
    requests_mock.post(
        f"{BASE}/values/'{SHEET}'!A1:append",
        json={"updates": {"updatedRange": f"'{SHEET}'!A5:C5"}},
    )

    row = client.append_row(SHEET, {"取引先ID": "CLI-005", "取引先名": "新規取引先"})

    assert row == 5
    sent_body = requests_mock.last_request.json()
    assert sent_body == {"values": [["CLI-005", "新規取引先", ""]]}


def test_append_row_raises_spreadsheet_api_error_when_updated_range_unparsable(
    requests_mock, client: HttpSpreadsheetClient
) -> None:
    requests_mock.get(f"{BASE}/values/'{SHEET}'!1:1", json={"values": [["取引先ID"]]})
    requests_mock.post(
        f"{BASE}/values/'{SHEET}'!A1:append", json={"updates": {"updatedRange": "invalid"}}
    )

    with pytest.raises(SpreadsheetApiError):
        client.append_row(SHEET, {"取引先ID": "CLI-005"})


def test_append_row_does_not_retry_on_5xx(
    requests_mock, client: HttpSpreadsheetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WARN対応: 作成系（非冪等）操作は5xxでもリトライせず即座にエラーとして返す
    （重複行追加を避ける）。
    """
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(f"{BASE}/values/'{SHEET}'!1:1", json={"values": [["取引先ID"]]})
    append_mock = requests_mock.post(
        f"{BASE}/values/'{SHEET}'!A1:append", status_code=500, json={"error": "boom"}
    )

    with pytest.raises(SpreadsheetApiError):
        client.append_row(SHEET, {"取引先ID": "CLI-005"})

    assert append_mock.call_count == 1


def test_append_row_raises_spreadsheet_api_error_on_4xx(
    requests_mock, client: HttpSpreadsheetClient
) -> None:
    requests_mock.get(f"{BASE}/values/'{SHEET}'!1:1", json={"values": [["取引先ID"]]})
    requests_mock.post(f"{BASE}/values/'{SHEET}'!A1:append", status_code=400, json={"error": "bad"})

    with pytest.raises(SpreadsheetApiError):
        client.append_row(SHEET, {"取引先ID": "CLI-005"})


# --- update_row --------------------------------------------------------------------------


def test_update_row_sends_batch_update_only_for_changed_columns(
    requests_mock, client: HttpSpreadsheetClient
) -> None:
    requests_mock.get(
        f"{BASE}/values/'{SHEET}'!1:1",
        json={"values": [["取引先ID", "取引先名", "顧客種別"]]},
    )
    requests_mock.post(f"{BASE}/values:batchUpdate", json={})

    client.update_row(SHEET, 5, {"取引先名": "更新後の名称"})

    sent_body = requests_mock.last_request.json()
    assert sent_body == {
        "valueInputOption": "USER_ENTERED",
        "data": [{"range": f"'{SHEET}'!B5", "values": [["更新後の名称"]]}],
    }


def test_update_row_skips_request_when_no_matching_columns(
    requests_mock, client: HttpSpreadsheetClient
) -> None:
    requests_mock.get(f"{BASE}/values/'{SHEET}'!1:1", json={"values": [["取引先ID"]]})
    batch_update_mock = requests_mock.post(f"{BASE}/values:batchUpdate", json={})

    client.update_row(SHEET, 5, {"存在しない列": "x"})

    assert batch_update_mock.call_count == 0


def test_update_row_raises_spreadsheet_api_error_on_5xx(
    requests_mock, client: HttpSpreadsheetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(f"{BASE}/values/'{SHEET}'!1:1", json={"values": [["取引先名"]]})
    requests_mock.post(f"{BASE}/values:batchUpdate", status_code=500, json={"error": "boom"})

    with pytest.raises(SpreadsheetApiError):
        client.update_row(SHEET, 5, {"取引先名": "更新後"})


# --- タイムアウト・リトライ ------------------------------------------------------------------


def test_get_row_retries_on_503_then_succeeds(
    requests_mock, client: HttpSpreadsheetClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.get(
        f"{BASE}/values:batchGet",
        [
            {"status_code": 503},
            {
                "json": {
                    "valueRanges": [
                        {"values": [["取引先ID"]]},
                        {"values": [["CLI-005"]]},
                    ]
                },
                "status_code": 200,
            },
        ],
    )

    row = client.get_row(SHEET, 5)

    assert row == {"取引先ID": "CLI-005"}
    assert requests_mock.call_count == 2


# --- column_letter -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "expected"),
    [(1, "A"), (2, "B"), (26, "Z"), (27, "AA"), (52, "AZ"), (53, "BA")],
)
def test_column_letter(index: int, expected: str) -> None:
    assert column_letter(index) == expected


def test_column_letter_rejects_non_positive_index() -> None:
    with pytest.raises(ValueError):
        column_letter(0)
