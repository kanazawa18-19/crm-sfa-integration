from datetime import datetime, timezone, timedelta
from unittest.mock import Mock
from src.db_schema.base import Tool
from src.sync_operations.exclusions import is_excluded
from src.sync_engine.dispatcher import Dispatcher, SyncEvent
from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
from tests.sync_engine.test_dispatcher import FakeSyncTarget


def test_only_confirmed_tool_database_and_two_ids_are_excluded():
    for identifier in ('62388', '62202'):
        assert is_excluded(Tool.KINTONE, 'client_master', identifier)
        assert not is_excluded(Tool.ZOHO, 'client_master', identifier)
        assert not is_excluded(Tool.KINTONE, 'project', identifier)
    assert not is_excluded(Tool.KINTONE, 'client_master', '62389')


def test_excluded_inbound_and_target_never_call_external_api():
    store = SQLiteIdMappingStore()
    dispatcher = Dispatcher(store, {})
    assert dispatcher.dispatch(SyncEvent(Tool.KINTONE, 'client_master', '62388', datetime.now(timezone.utc), {})).reason == 'approved_record_exclusion'
    client = Mock(); target = KintoneSyncTarget(client, 'client-app')
    assert target.get_record('62388', db_key='client_master') is None
    assert target.upsert_record('62202', {'取引先名': '保存しない'}, db_key='client_master') is None
    assert not client.mock_calls


def test_excluded_destination_does_not_block_other_tools_or_report_retry():
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping('source-page', 'client_master', zoho_id='z1', kintone_id='62388'))
    client = Mock(); target = KintoneSyncTarget(client, 'client-app')
    zoho = FakeSyncTarget(Tool.ZOHO, {'z1': {'取引先名': '旧名'}})
    dispatcher = Dispatcher(store, {Tool.KINTONE: target, Tool.ZOHO: zoho})
    result = dispatcher.dispatch(SyncEvent(Tool.NOTION, 'client_master', 'source-page', datetime.now(timezone.utc), {'取引先名': '新名'}))
    assert zoho.upsert_calls == [('z1', {'取引先名': '新名'})]
    assert not client.mock_calls
    assert all(Tool.KINTONE not in item.skipped_tools for item in result.properties)


def test_external_update_ignores_excluded_destination_without_blocking_notion():
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping('source-page', 'client_master', zoho_id='z1', kintone_id='62202'))
    client = Mock(); target = KintoneSyncTarget(client, 'client-app')
    from src.sync_engine.clients.notion_client import NOTION_LAST_EDITED_TIME_KEY
    notion = FakeSyncTarget(Tool.NOTION, {'source-page': {'取引先名': '旧名', NOTION_LAST_EDITED_TIME_KEY: datetime.now(timezone.utc) - timedelta(days=1)}})
    dispatcher = Dispatcher(store, {Tool.KINTONE: target, Tool.NOTION: notion})
    result = dispatcher.dispatch(SyncEvent(Tool.ZOHO, 'client_master', 'z1', datetime.now(timezone.utc), {'取引先名': '新名'}))
    assert notion.upsert_calls == [('source-page', {'取引先名': '新名'})]
    assert not client.mock_calls
    assert all(Tool.KINTONE not in item.skipped_tools for item in result.properties)


def test_本人がZohoへ作らないと決めた2件も他の配送を維持():
    from src.sync_operations.exclusions import is_destination_excluded
    for identifier in ('62543', '62580'):
        mapping = IdMapping('source-page', 'client_master', kintone_id=identifier)
        assert is_destination_excluded(Tool.ZOHO, mapping)
        assert not is_destination_excluded(Tool.KINTONE, mapping)
        assert not is_destination_excluded(Tool.SPREADSHEET, mapping)
        assert not is_destination_excluded(Tool.ZOHO, IdMapping('other', 'project', kintone_id=identifier))
        assert not is_destination_excluded(Tool.ZOHO, IdMapping('other', 'client_master', kintone_id=identifier, zoho_id='existing'))
        store = SQLiteIdMappingStore()
        store.upsert(mapping)
        kintone = FakeSyncTarget(Tool.KINTONE, {identifier: {'TEL': 'old'}})
        sheet = FakeSyncTarget(Tool.SPREADSHEET, {})
        result = Dispatcher(store, {Tool.KINTONE: kintone, Tool.SPREADSHEET: sheet}).dispatch(
            SyncEvent(Tool.NOTION, 'client_master', 'source-page', datetime.now(timezone.utc), {'TEL': 'new'}))
        assert kintone.upsert_calls == [(identifier, {'TEL': 'new'})]
        assert all(Tool.ZOHO not in item.skipped_tools for item in result.properties)


def test_逆参照と接触日は実際に対応する同期先だけへ配送():
    for db_key, field, value, expected_tools in [
        ('project', 'アクション履歴', ['action-page'], {Tool.SPREADSHEET}),
        ('client_master', '【営業部・パーソネル】アクション履歴DB', ['action-page'], {Tool.SPREADSHEET}),
        ('action', 'アクション日', '2026-10-13', {Tool.SPREADSHEET, Tool.ZOHO}),
    ]:
        store = SQLiteIdMappingStore()
        store.upsert(IdMapping('source-page', db_key, zoho_id='z', kintone_id='k', spreadsheet_row=2))
        targets = {tool: FakeSyncTarget(tool, {}) for tool in (Tool.KINTONE, Tool.ZOHO, Tool.SPREADSHEET)}
        result = Dispatcher(store, targets).dispatch(SyncEvent(
            Tool.NOTION, db_key, 'source-page', datetime.now(timezone.utc), {field: value}))
        assert {tool for tool, target in targets.items() if target.upsert_calls} == expected_tools
        assert result.properties[0].skipped_tools == frozenset()
