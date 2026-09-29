from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from src.record_merge.domain import MergeHeld
from src.record_merge.relation_decisions import RelationDecisions, retain_relations
from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
from src.db_schema.base import Tool
from tests.record_merge.test_gateway import raw


def test_related_candidates_keep_other_relations_and_enforce_full_limit():
    assert retain_relations(['other'], 'chosen') == ['other', 'chosen']
    assert retain_relations(['chosen', 'other'], 'chosen') == ['chosen', 'other']
    assert len(retain_relations([str(i) for i in range(99)], 'chosen')) == 100
    for values in ([str(i) for i in range(100)], None, [None]):
        with pytest.raises(MergeHeld): retain_relations(values, 'chosen')


def test_comparison_keeps_unrelated_ids_and_checks_current_external_mapping(monkeypatch):
    import src.record_merge.relation_decisions as module
    monkeypatch.setattr(module, 'resolve_alias', lambda *args: None)
    monkeypatch.setattr(module, 'acquire_record_sync_lock', lambda *args: nullcontext())
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping('chosen', 'client_master', zoho_id='z1'))
    pages = {'chosen': raw('client_master', 'chosen', {'取引先名': '候補施設'}),
             'source': raw('action', 'source', {'👨‍👩‍👧‍👦 取引先マスター': ['other']})}
    gateway = SimpleNamespace(page=lambda db, page: pages[page])
    service = RelationDecisions(SimpleNamespace(store=store, dispatcher=Mock(), gateway=gateway))
    service.queue_row = lambda *args: {'targetDbKey': 'client_master', 'rawValue': '候補施設', 'candidateNotionPageIds': ['chosen']}
    service.source_options = lambda row: [{'dbKey': 'action', 'notionId': 'source', 'property': '👨‍👩‍👧‍👦 取引先マスター', 'native': {'id': 'z1', 'name': '候補施設'}}]
    preview = service.compare('manager', 'review', 'chosen')
    assert preview['snapshot']['before'] == ['other']
    assert preview['snapshot']['desired'] == ['other', 'chosen']
    monkeypatch.setattr(module, 'require_record_available', lambda *args: None)
    store.upsert(IdMapping('chosen', 'client_master', zoho_id='changed'))
    with pytest.raises(MergeHeld, match='外部ID対応'):
        service._execute('manager', preview['snapshot'])


def test_explicit_source_db_does_not_read_other_apps_with_same_record_number():
    store = Mock()
    store.find_by_external_id.return_value = IdMapping('source', 'action', kintone_id='10')
    target = Mock()
    target.get_record.return_value = {'client_name': '候補施設', '$revision': '5'}
    service = RelationDecisions(SimpleNamespace(
        store=store, dispatcher=SimpleNamespace(_targets={Tool.KINTONE: target}), gateway=Mock(),
    ))
    row = {'sourceTool': 'kintone', 'sourceRecordId': '10',
           'targetDbKey': 'client_master', 'rawValue': '候補施設'}
    options = service.source_options(row, source_db='action')
    assert len(options) == 1
    assert options[0]['dbKey'] == 'action'
    store.find_by_external_id.assert_called_once_with(Tool.KINTONE, '10', db_key='action')
    target.get_record.assert_called_once_with('10', db_key='action')
    row['rawValue'] = '別施設'
    with pytest.raises(MergeHeld, match='現在値'):
        service.source_options(row, source_db='action')


def test_legacy_lookup_matches_id_and_rejects_changed_id():
    from src.record_merge.relation_decisions import matches_pending_value
    value = {'name': '現在の名称', 'id': '123'}
    assert matches_pending_value("{'name': '旧名称', 'id': '123'}", value)
    assert not matches_pending_value("{'name': '現在の名称', 'id': '999'}", value)
    assert not matches_pending_value("__import__('os').system('false')", value)
    assert not matches_pending_value('[' * 5000, value)


def test_project_product_source_uses_existing_lookup_id():
    store = Mock()
    store.find_by_external_id.return_value = IdMapping('source', 'project', zoho_id='10')
    target = Mock()
    target.get_record.return_value = {'field72': {'name': '商品', 'id': '20'}}
    service = RelationDecisions(SimpleNamespace(
        store=store, dispatcher=SimpleNamespace(_targets={Tool.ZOHO: target}), gateway=Mock(),
    ))
    row = {'sourceTool': 'zoho', 'sourceRecordId': '10', 'targetDbKey': 'product', 'rawValue': '商品'}
    options = service.source_options(row, source_db='project')
    assert len(options) == 1
    assert options[0]['field'] == 'field72'
    assert options[0]['native']['id'] == '20'
