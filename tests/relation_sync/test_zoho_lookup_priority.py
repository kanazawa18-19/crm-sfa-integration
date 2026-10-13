"""同名取引先があっても、確定したZoho IDから別会社へ逸れない。"""
import pytest

from src.db_schema.base import Tool
from src.relation_sync import resolve_zoho
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.webhook_handlers import zoho_field_transforms
from src.sync_engine.webhook_handlers.zoho_webhook import zoho_payload_to_sync_events


@pytest.mark.parametrize('module,field,property_name', [
    ('CustomModule2', 'field6', '👨‍👩‍👧‍👦 取引先マスター'),
    ('Contacts', 'field25', '取引先マスター'),
])
@pytest.mark.parametrize('mapping_state', ['matched', 'missing', 'failed', 'disabled'])
def test_ID付き通知は名前による候補へ切り替えない(monkeypatch, module, field, property_name, mapping_state):
    monkeypatch.setenv('RELATION_SYNC_ENABLED', 'false' if mapping_state == 'disabled' else 'true')
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key='correct-client', db_key='client_master', zoho_id='900'))
    store.upsert(IdMapping(notion_key='other-client', db_key='client_master', zoho_id='901'))
    name_calls = []
    fallback = lambda *args, **kwargs: name_calls.append((args, kwargs)) or 'other-client'
    monkeypatch.setattr(resolve_zoho, 'resolve_client_master_relation', fallback)
    monkeypatch.setattr(zoho_field_transforms, 'resolve_client_master_relation', fallback)
    reviews = []
    monkeypatch.setattr(resolve_zoho, 'enqueue_for_review', lambda **kwargs: reviews.append(kwargs))
    lookups = []
    original = store.find_by_external_id
    def lookup(tool, external_id, *, db_key):
        lookups.append((tool, external_id, db_key))
        if mapping_state == 'failed':
            raise RuntimeError('対応表を読めない')
        return original(tool, external_id, db_key=db_key)
    monkeypatch.setattr(store, 'find_by_external_id', lookup)
    target_id = '999' if mapping_state == 'missing' else '900'
    events = zoho_payload_to_sync_events(
        {'module': module, 'ids': ['source'], 'server_time': 1791860400000,
         'affected_values': [{'record_id': 'source', 'values': {field: {'name': '同名会社', 'id': target_id}}}]},
        {}, id_mapping_store=store,
    )
    assert len(events) == 1
    assert name_calls == []
    if mapping_state == 'matched':
        assert events[0].properties[property_name] == 'correct-client'
        assert reviews == []
    else:
        assert property_name not in events[0].properties
    if mapping_state == 'disabled':
        assert lookups == []
    else:
        assert lookups == [(Tool.ZOHO, target_id, 'client_master')]
    if mapping_state == 'missing':
        assert len(reviews) == 1
        assert reviews[0]['source_record_id'] == 'source'


def test_既定のNotion直接参照の優先順位を維持する(monkeypatch):
    monkeypatch.setenv('RELATION_SYNC_ENABLED', 'true')
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key='lookup-client', db_key='client_master', zoho_id='900'))
    hint = '0123456789abcdef0123456789abcdef'
    assert resolve_zoho.resolve_zoho_action_client_master_relation(
        record_id='source', changed_values={'field22': f'https://www.notion.so/{hint}',
                                           'field6': {'name': '同名会社', 'id': '900'}},
        zoho_client=None, id_mapping_store=store,
    ) == hint


@pytest.mark.parametrize('mapped', [True, False])
def test_部分通知でも現在の取引先IDを補って照合する(monkeypatch, mapped):
    monkeypatch.setenv('RELATION_SYNC_ENABLED', 'true')
    store = SQLiteIdMappingStore()
    if mapped:
        store.upsert(IdMapping(notion_key='correct-client', db_key='client_master', zoho_id='900'))
    name_calls = []
    monkeypatch.setattr(resolve_zoho, 'resolve_client_master_relation',
                        lambda *args, **kwargs: name_calls.append(args) or 'wrong-client')
    reviews = []
    monkeypatch.setattr(resolve_zoho, 'enqueue_for_review', lambda **kwargs: reviews.append(kwargs))
    calls = []
    class Client:
        def get_record(self, module, record_id):
            calls.append((module, record_id))
            return {'field6': {'name': '同名会社', 'id': '900'}}
    result = resolve_zoho.resolve_zoho_action_client_master_relation(
        record_id='source', changed_values={'field22': ''},
        zoho_client=Client(), id_mapping_store=store,
    )
    assert result == ('correct-client' if mapped else None)
    assert calls == [('CustomModule2', 'source')]
    assert name_calls == []
    assert len(reviews) == (0 if mapped else 1)
