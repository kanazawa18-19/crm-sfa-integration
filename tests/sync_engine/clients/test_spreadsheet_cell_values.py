"""セル値の変換（`_to_cell_value`、2026-08-31）。

Sheetsはセルにスカラーしか受け付けない。リレーション（NotionページIDの配列）を
そのまま渡すと400になり、**その行だけでなくバッチ全体が失敗する**。
バックフィルを`--apply`して初めて出た（試算では気づけなかった）。
"""

from __future__ import annotations

from src.sync_engine.clients.spreadsheet_client import _to_cell_value


def test_relation_lists_become_a_joined_string() -> None:
    assert _to_cell_value(["page-a", "page-b"]) == "page-a, page-b"


def test_empty_list_becomes_an_empty_cell() -> None:
    assert _to_cell_value([]) == ""


def test_lookup_dict_prefers_the_readable_name() -> None:
    assert _to_cell_value({"name": "ホテルユクエスタ旭橋", "id": "2233"}) == "ホテルユクエスタ旭橋"


def test_dict_without_a_name_is_kept_as_json() -> None:
    assert _to_cell_value({"id": "2233"}) == '{"id": "2233"}'


def test_none_becomes_an_empty_cell() -> None:
    assert _to_cell_value(None) == ""


def test_scalars_pass_through_unchanged() -> None:
    assert _to_cell_value("A社") == "A社"
    assert _to_cell_value(0) == 0
    assert _to_cell_value(1500.5) == 1500.5
    assert _to_cell_value(False) is False


def test_primed_cache_stops_re_reading_the_column(requests_mock) -> None:
    """バックフィル中は同期キー列を1回しか読まない（O(n²)を避ける）。"""
    from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient

    spreadsheet_id = "sheet-abc123"
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    sheet = "取引先マスター"
    client = HttpSpreadsheetClient(spreadsheet_id, access_token="t")

    requests_mock.get(f"{base}/values/'{sheet}'!1:1", json={"values": [["名前", "同期キー"]]})
    column = requests_mock.get(
        f"{base}/values/'{sheet}'!B:B", json={"values": [["同期キー", "key-a"]]}
    )

    assert client.prime_sync_key_rows(sheet, "同期キー") == 1
    assert client.find_row_by_sync_key(sheet, "同期キー", "key-a") == 2
    # 先読み以降、見つからないキーで列を読み直さないこと。
    assert client.find_row_by_sync_key(sheet, "同期キー", "key-missing") is None
    assert client.find_row_by_sync_key(sheet, "同期キー", "key-missing2") is None
    assert column.call_count == 1


def test_without_priming_a_miss_still_re_reads(requests_mock) -> None:
    """通常運用では、見つからないで終わる前に必ず実データを読む（重複行を作らないため）。"""
    from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient

    spreadsheet_id = "sheet-abc123"
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    sheet = "取引先マスター"
    client = HttpSpreadsheetClient(spreadsheet_id, access_token="t")

    requests_mock.get(f"{base}/values/'{sheet}'!1:1", json={"values": [["名前", "同期キー"]]})
    column = requests_mock.get(
        f"{base}/values/'{sheet}'!B:B", json={"values": [["同期キー", "key-a"]]}
    )

    assert client.find_row_by_sync_key(sheet, "同期キー", "key-missing") is None
    assert client.find_row_by_sync_key(sheet, "同期キー", "key-missing") is None
    assert column.call_count == 2


def test_primed_sheet_reads_the_header_only_once(requests_mock) -> None:
    """ヘッダ行の読み直しでQuotaに当たっていた（1件24秒まで落ちた）。"""
    from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient

    spreadsheet_id = "sheet-abc123"
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    sheet = "取引先マスター"
    client = HttpSpreadsheetClient(spreadsheet_id, access_token="t")

    header = requests_mock.get(
        f"{base}/values/'{sheet}'!1:1", json={"values": [["名前", "同期キー"]]}
    )
    requests_mock.get(f"{base}/values/'{sheet}'!B:B", json={"values": [["同期キー"]]})
    requests_mock.post(
        f"{base}/values/'{sheet}'!A1:append",
        json={"updates": {"updatedRange": f"'{sheet}'!A2:B2"}},
    )

    client.prime_sync_key_rows(sheet, "同期キー")
    before = header.call_count
    client.append_row(sheet, {"名前": "A社"})
    client.append_row(sheet, {"名前": "B社"})

    assert header.call_count == before


def test_header_is_re_read_when_not_primed(requests_mock) -> None:
    """通常運用ではキャッシュしない（人が列を足すことがあるため）。"""
    from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient

    spreadsheet_id = "sheet-abc123"
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    sheet = "取引先マスター"
    client = HttpSpreadsheetClient(spreadsheet_id, access_token="t")

    header = requests_mock.get(
        f"{base}/values/'{sheet}'!1:1", json={"values": [["名前"]]}
    )
    requests_mock.post(
        f"{base}/values/'{sheet}'!A1:append",
        json={"updates": {"updatedRange": f"'{sheet}'!A2:A2"}},
    )

    client.append_row(sheet, {"名前": "A社"})
    client.append_row(sheet, {"名前": "B社"})

    assert header.call_count == 2


def test_append_rows_writes_one_request_and_returns_row_numbers(requests_mock) -> None:
    """まとめ追記。1行ずつだとSheetsのQuotaで1秒1行になり、3万件で18時間かかる。"""
    from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient

    spreadsheet_id = "sheet-abc123"
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}"
    sheet = "取引先マスター"
    client = HttpSpreadsheetClient(spreadsheet_id, access_token="t")

    requests_mock.get(f"{base}/values/'{sheet}'!1:1", json={"values": [["名前", "同期キー"]]})
    post = requests_mock.post(
        f"{base}/values/'{sheet}'!A1:append",
        json={"updates": {"updatedRange": f"'{sheet}'!A5:B7"}},
    )

    rows = client.append_rows(
        sheet,
        [
            {"名前": "A社", "同期キー": "k1"},
            {"名前": "B社", "同期キー": "k2"},
            {"名前": "C社", "同期キー": "k3"},
        ],
    )

    assert rows == [5, 6, 7]
    assert post.call_count == 1
    assert post.last_request.json() == {
        "values": [["A社", "k1"], ["B社", "k2"], ["C社", "k3"]]
    }


def test_append_rows_with_nothing_makes_no_request(requests_mock) -> None:
    from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient

    client = HttpSpreadsheetClient("sheet-abc123", access_token="t")

    assert client.append_rows("取引先マスター", []) == []
