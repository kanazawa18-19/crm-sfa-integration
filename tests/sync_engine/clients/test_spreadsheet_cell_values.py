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
