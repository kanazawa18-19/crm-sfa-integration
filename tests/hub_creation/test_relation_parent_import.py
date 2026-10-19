"""取込予約の前後で通常通知が別のCRM登録を始めないことを確認する。"""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import Mock
import pytest
from src.db_schema.base import Tool
from src.sync_engine.id_mapping import IdMapping
from src.sync_engine.sync_event import SyncEvent
from tests.hub_creation.test_service import setup
from tests.hub_creation.test_scan_postgres import local  # noqa: F401


@pytest.mark.parametrize('stage', ['timeout', 'before_id_saved', 'before_mapping', 'after_mapping'])
def test_notion_notifications_never_start_external_creation(stage):
    service, event, journal, notion, adapter, store = setup()
    service.notion_clients = {'client_master': notion}
    event = replace(event, db_key='client_master')
    key = 'relation-parent-import:zoho:123'
    journal.rows[key, 'notion'] = {'state': 'reserved', 'identityHash': 'plan'}
    if stage in ('before_mapping', 'after_mapping'):
        journal.rows[key, 'notion'].update(state='created', externalId='page')
    if stage == 'after_mapping':
        store.upsert(IdMapping('page', 'client_master', zoho_id='123'))
    reason = service.handle(event)
    assert reason == (None if stage == 'after_mapping' else 'relation_parent_import_pending')
    assert adapter.creates == 0
    assert journal.claims == []


def test_unrelated_existing_notion_record_is_not_held():
    service, event, journal, notion, adapter, store = setup()
    journal.rows['relation-parent-import:zoho:123', 'notion'] = {'state': 'reserved'}
    store.upsert(IdMapping('page', 'chain', zoho_id='other'))
    assert service.handle(event) is None
    assert adapter.creates == 0


@pytest.mark.parametrize('stage', ['reserved', 'created'])
def test_real_postgres_reservation_blocks_zoho_guard_even_when_creation_disabled(local, monkeypatch, stage):
    _, journal = local
    key = 'relation-parent-import:zoho:123'
    assert journal.reserve(key, 'notion', 'client_master', 'plan')
    if stage == 'created': journal.finish(key, 'notion', 'parent')
    assert journal.is_relation_parent_import('client_master', 'zoho', '123')
    assert not journal.is_relation_parent_import('client_master', 'zoho', 'other')
    assert not journal.is_relation_parent_import('action', 'zoho', '123')
    assert journal.is_relation_parent_import('client_master', 'notion', 'parent', include_unidentified=True)
    assert journal.is_relation_parent_import('client_master', 'notion', 'parent') == (stage == 'created')
    assert journal.is_relation_parent_import('client_master', 'notion', 'other', include_unidentified=True) == (stage == 'reserved')
    from src.sync_engine import production_wiring as wiring
    from src.sync_engine.id_mapping import SQLiteIdMappingStore
    monkeypatch.setattr(wiring, 'build_notion_clients_by_db', lambda *a, **kw: {})
    monkeypatch.setattr(wiring, 'build_kintone_targets_by_db', lambda *a, **kw: {})
    monkeypatch.setattr(wiring, 'build_zoho_targets_by_db', lambda *a, **kw: {})
    monkeypatch.setattr(wiring, 'build_spreadsheet_targets_by_db', lambda *a, **kw: {})
    monkeypatch.delenv('HUB_CREATION_ENABLED_SINCE', raising=False)
    dispatcher = wiring.build_production_dispatcher(id_mapping_store=SQLiteIdMappingStore(), slack_notifier=None)
    event = SyncEvent(Tool.ZOHO, 'client_master', '123', datetime.now(timezone.utc))
    assert dispatcher._new_record_guard(event, {}) is True


def test_mapped_zoho_update_bypasses_creation_guard_and_reaches_notion():
    from src.sync_engine.dispatcher import Dispatcher
    from src.sync_engine.id_mapping import SQLiteIdMappingStore
    from tests.sync_engine.test_dispatcher import FakeSyncTarget
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping('parent', 'client_master', zoho_id='123'))
    notion = FakeSyncTarget(Tool.NOTION, records={'parent': {'取引先名': None}})
    guard = Mock(side_effect=AssertionError('既存更新で作成ガードを呼んではいけない'))
    dispatcher = Dispatcher(store, {Tool.NOTION: notion}, new_record_guard=guard)
    result = dispatcher.dispatch(SyncEvent(Tool.ZOHO, 'client_master', '123', datetime.now(timezone.utc),
                                          properties={'取引先名': '更新後の名称'}))
    assert not result.skipped
    guard.assert_not_called()
    assert notion.upsert_calls == [('parent', {'取引先名': '更新後の名称'})]
    store.close()
