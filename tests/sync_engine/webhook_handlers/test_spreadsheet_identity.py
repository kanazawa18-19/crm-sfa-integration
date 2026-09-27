"""シートを並べ替えても別のNotionページへ書かないことを検証する。"""
from datetime import datetime, timezone
import pytest
from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher, DispatchResult
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.webhook_handlers.spreadsheet_webhook import spreadsheet_payload_to_sync_event

A='11111111-1111-4111-8111-111111111111'
B='22222222-2222-4222-8222-222222222222'


def payload(key):
    return {'sheet':'取引先マスタ','row':2,'editedAt':'2026-09-27T12:00:00Z',
            'values':{'同期キー':key,'取引先名':'編集後'}}


def event(key):
    return spreadsheet_payload_to_sync_event(payload(key),{},sheet_to_db_key={'取引先マスタ':'client_master'})


def test_reordered_row_dispatches_to_its_key_not_previous_occupant():
    class Capture(Dispatcher):
        def _dispatch_locked(self,event,mapping,guard):
            self.selected=mapping.notion_key
            return DispatchResult(skipped=False)
    with_store=SQLiteIdMappingStore(':memory:')
    try:
        with_store.upsert(IdMapping(notion_key=A,db_key='client_master',spreadsheet_row=2))
        with_store.upsert(IdMapping(notion_key=B,db_key='client_master',spreadsheet_row=3))
        dispatcher=Capture(with_store,{})
        assert not dispatcher.dispatch(event(B)).skipped
        assert dispatcher.selected==B
        assert '同期キー' not in event(B).properties
    finally:with_store.close()


@pytest.mark.parametrize('key',[None,'','not-a-page',123,{},[]])
def test_missing_or_invalid_key_never_falls_back_to_row(key):
    with pytest.raises(ValueError,match='同期キー'):
        event(key)


def test_compact_uuid_is_normalized():
    assert event(B.replace('-','').upper()).source_notion_key==B


def test_unknown_key_wrong_db_and_legacy_event_are_not_resolved():
    store=SQLiteIdMappingStore(':memory:')
    try:
        store.upsert(IdMapping(notion_key=A,db_key='client_master',spreadsheet_row=2))
        store.upsert(IdMapping(notion_key=B,db_key='project',spreadsheet_row=2))
        dispatcher=Dispatcher(store,{})
        assert dispatcher.dispatch(event(B)).reason=='unknown_record'
        assert dispatcher.dispatch(event('33333333-3333-4333-8333-333333333333')).reason=='unknown_record'
        legacy=SyncEvent(Tool.SPREADSHEET,'client_master','2',datetime.now(timezone.utc))
        assert dispatcher.dispatch(legacy).reason=='unknown_record'
    finally:store.close()


def test_duplicate_sheet_key_rejected_before_any_dispatch_write():
    from unittest.mock import Mock
    from src.sync_engine.clients._http import ApiError
    store = SQLiteIdMappingStore(':memory:')
    try:
        store.upsert(IdMapping(notion_key=A, db_key='client_master', spreadsheet_row=2))
        sheet = Mock()
        sheet.get_record_by_sync_key.side_effect = ApiError(409, '同期キーが重複')
        notion = Mock()
        dispatcher = Dispatcher(store, {Tool.SPREADSHEET: sheet, Tool.NOTION: notion})
        with pytest.raises(ApiError, match='重複'):
            dispatcher.dispatch(event(A))
        notion.upsert_record.assert_not_called()
        sheet.upsert_record.assert_not_called()
        assert store.get(A).last_synced_at is None
    finally:
        store.close()
