"""実HTTPを使わず、行移動・複製・読取中の移動を再現する。"""
from unittest.mock import Mock

import pytest

from src.sync_engine.clients._http import ApiError
from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient, SpreadsheetApiError
from src.sync_engine.sync_targets.spreadsheet_sync import SpreadsheetSyncTarget
from src.sync_engine.dispatcher import Dispatcher
from src.db_schema.base import Tool
from src.sync_engine.id_mapping import IdMapping

KEY = "11111111-1111-4111-8111-111111111111"
BASE = "https://sheets.googleapis.com/v4/spreadsheets/test-sheet"


@pytest.mark.parametrize("cells, expected", [(["同期キー", "other", KEY], 3), (["同期キー"], None)])
def test_unique_lookup_ignores_stale_cache_and_only_reads(requests_mock, cells, expected):
    client = HttpSpreadsheetClient("test-sheet", access_token="test-token")
    client._sync_key_rows["取引先"] = {KEY: 42}
    requests_mock.get(BASE + "/values/'取引先'!1:1", json={"values": [["同期キー", "名前"]]})
    requests_mock.get(BASE + "/values/'取引先'!A:A", json={"values": [cells]})
    assert client.find_unique_row_by_sync_key("取引先", "同期キー", KEY) == expected
    assert all(r.method == "GET" for r in requests_mock.request_history)


def test_duplicate_compact_key_fails_closed(requests_mock):
    client = HttpSpreadsheetClient("test-sheet", access_token="test-token")
    requests_mock.get(BASE + "/values/'取引先'!1:1", json={"values": [["同期キー"]]})
    requests_mock.get(BASE + "/values/'取引先'!A:A", json={"values": [["同期キー", KEY, KEY.replace("-", "").upper()]]})
    with pytest.raises(SpreadsheetApiError, match="重複"):
        client.find_unique_row_by_sync_key("取引先", "同期キー", KEY)
    assert all(r.method == "GET" for r in requests_mock.request_history)


def test_missing_header_does_not_create_column(requests_mock):
    client = HttpSpreadsheetClient("test-sheet", access_token="test-token")
    requests_mock.get(BASE + "/values/'取引先'!1:1", json={"values": [["名前"]]})
    with pytest.raises(SpreadsheetApiError, match="列"):
        client.find_unique_row_by_sync_key("取引先", "同期キー", KEY)
    assert requests_mock.call_count == 1


def test_move_between_key_lookup_and_row_read_is_rejected():
    client = Mock()
    client.find_unique_row_by_sync_key.return_value = 3
    client.get_row.return_value = {"同期キー": "another-page", "名前": "他の行"}
    target = SpreadsheetSyncTarget(client, "取引先", "client_master")
    with pytest.raises(ApiError, match="行が変わった"):
        target.get_record_by_sync_key(KEY)
    client.update_row.assert_not_called()


def test_snapshot_reads_by_key_instead_of_saved_row():
    target = Mock()
    target.get_record_by_sync_key.return_value = {"同期キー": KEY, "名前": "正しい行"}
    mapping = IdMapping(notion_key=KEY, db_key="client_master", spreadsheet_row=42)
    assert Dispatcher._read_mapped_record(Tool.SPREADSHEET, target, mapping, "42")["名前"] == "正しい行"
    target.get_record.assert_not_called()
    target.get_record_by_sync_key.assert_called_once_with(KEY, db_key="client_master")


@pytest.mark.parametrize('actual', [None, '', '別のキー'])
def test_write_guard_rejects_empty_or_changed_key(actual):
    from src.sync_engine.production_wiring import _MultiDbSpreadsheetSyncTarget
    client = Mock()
    client.get_row.return_value = {'同期キー': actual, '名前': '他人の行'}
    target = _MultiDbSpreadsheetSyncTarget({'client_master': SpreadsheetSyncTarget(client, '取引先', 'client_master')})
    with pytest.raises(ApiError, match='書込直前'):
        target.update_with_sync_key('3', {'名前': '更新'}, KEY, db_key='client_master')
    client.update_row.assert_not_called()


def test_write_guard_accepts_equivalent_uuid_notation():
    from src.sync_engine.production_wiring import _MultiDbSpreadsheetSyncTarget
    client = Mock()
    client.get_row.return_value = {'同期キー': KEY.replace('-', '').upper()}
    target = _MultiDbSpreadsheetSyncTarget({'client_master': SpreadsheetSyncTarget(client, '取引先', 'client_master')})
    assert target.update_with_sync_key('3', {'取引先名': '更新'}, KEY, db_key='client_master') == '3'
    client.update_row.assert_called_once()


def test_missing_saved_row_still_reads_sheet_version_by_key():
    from src.sync_engine.id_mapping import SQLiteIdMappingStore
    store = SQLiteIdMappingStore(':memory:')
    try:
        target = Mock()
        target.get_record_by_sync_key.return_value = {'同期キー': KEY}
        mapping = IdMapping(notion_key=KEY, db_key='client_master', spreadsheet_row=None)
        dispatcher = Dispatcher(store, {Tool.SPREADSHEET: target})
        assert dispatcher._fetch_one_version(Tool.SPREADSHEET, mapping) == {'同期キー': KEY}
        target.get_record.assert_not_called()
    finally:
        store.close()


def test_sheet_has_no_version_so_version_prefetch_does_not_read():
    from src.sync_engine.id_mapping import SQLiteIdMappingStore
    store = SQLiteIdMappingStore(':memory:')
    try:
        target = Mock()
        mapping = IdMapping(notion_key=KEY, db_key='client_master', spreadsheet_row=42)
        dispatcher = Dispatcher(store, {Tool.SPREADSHEET: target})
        assert dispatcher._fetch_versions(frozenset({Tool.SPREADSHEET}), mapping) == {}
        target.get_record.assert_not_called()
        target.get_record_by_sync_key.assert_not_called()
    finally:
        store.close()
