from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from src.record_merge.domain import MergeHeld
from src.record_merge.relation_decisions import RelationDecisions, retain_relations
from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
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
