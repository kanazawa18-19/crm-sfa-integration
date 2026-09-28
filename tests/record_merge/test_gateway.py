from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from src.db_schema.registry import ALL_SCHEMAS, get_schema
from src.record_merge.application import MergeService
from src.record_merge.notion_gateway import NotionMergeGateway
from src.sync_engine.clients.notion_client import build_notion_properties
from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
from tests.record_merge.test_application import Journal


class Client:
    def __init__(self, db, pages): self.db, self.pages = db, pages
    def get_raw_page_with_relations(self, page, names): return deepcopy(self.pages[page])
    def _request(self, method, path):
        return SimpleNamespace(status_code=200, ok=True, json=lambda: {'properties': {}} if '/databases/' in path else {'results': [], 'has_more': False})
    def query_raw(self, body):
        prop = body['filter']['property']; identifier = body['filter']['relation']['contains']
        results = [deepcopy(row) for row in self.pages.values() if not row.get('archived') and
            any(item['id'] == identifier for item in row['properties'].get(prop, {}).get('relation', []))]
        return {'results': results, 'has_more': False, 'request_status': None}
    def update_page(self, page, properties):
        self.pages[page]['properties'].update(build_notion_properties(properties, get_schema(self.db)))
        for prop in self.pages[page]['properties'].values():
            if 'type' not in prop: prop['type'] = next(iter(prop))
            for item in prop.get('title', prop.get('rich_text', [])):
                item['plain_text'] = item.get('text', {}).get('content', '')
    def archive_page(self, page): self.pages[page]['archived'] = True


def raw(db, page, properties):
    props = build_notion_properties(properties, get_schema(db))
    for prop in props.values():
        prop['type'] = next(iter(prop))
        for item in prop.get('title', prop.get('rich_text', [])):
            item['plain_text'] = item.get('text', {}).get('content', '')
    return {'id': page, 'properties': props, 'parent': {'database_id': get_schema(db).notion_database_id},
            'last_edited_time': '2026-09-28T00:00:00Z', 'archived': False}


def test_complete_merge_preserves_target_values_and_child_before_archiving():
    source = raw('client_master', 'source', {'取引先名': '同名施設', 'TEL': '0123'})
    target = raw('client_master', 'target', {'取引先名': '同名施設', 'TEL': ''})
    child = raw('action', 'child', {'👨‍👩‍👧‍👦 取引先マスター': ['source', 'other']})
    clients = {schema.key: Client(schema.key, {}) for schema in ALL_SCHEMAS}
    clients['client_master'].pages = {'source': source, 'target': target}
    clients['action'].pages = {'child': child}
    store = SQLiteIdMappingStore(); store.upsert(IdMapping('target', 'client_master'))
    gateway = NotionMergeGateway(clients, store)
    snapshot = gateway.snapshot('client_master', 'source', 'target')
    steps = gateway.plan(snapshot, {})
    journal = Journal(steps)
    journal.job.update(dbKey='client_master', sourceId='source', targetId='target', snapshot=snapshot)
    assert MergeService(journal, gateway, lambda job: nullcontext()).execute('op', 'manager')['state'] == 'done'
    assert source['archived'] is True
    assert target['properties']['TEL']['phone_number'] == '0123'
    assert child['properties']['👨‍👩‍👧‍👦 取引先マスター']['relation'] == [{'id': 'target'}, {'id': 'other'}]
    assert journal.aliases


def test_dual_relation_updates_are_planned_without_ignoring_third_party_edits():
    parent_field = next(prop.name for prop in get_schema('client_master').properties if prop.relation_target == 'action')
    child_field = '👨‍👩‍👧‍👦 取引先マスター'
    source = raw('client_master', 'source', {'取引先名': '同名施設', parent_field: ['child']})
    target = raw('client_master', 'target', {'取引先名': '同名施設', parent_field: []})
    child = raw('action', 'child', {child_field: ['source']})
    clients = {schema.key: Client(schema.key, {}) for schema in ALL_SCHEMAS}
    parents = clients['client_master']; parents.pages = {'source': source, 'target': target}
    actions = clients['action']; actions.pages = {'child': child}
    original_parent_write, original_child_write = parents.update_page, actions.update_page
    def parent_write(page, properties):
        original_parent_write(page, properties)
        for child_id in properties.get(parent_field, []):
            refs = child['properties'][child_field]['relation']
            if {'id': page} not in refs: refs.append({'id': page})
    def child_write(page, properties):
        original_child_write(page, properties)
        for parent_id, parent in parents.pages.items():
            refs = parent['properties'][parent_field]['relation']
            should_contain = parent_id in properties[child_field]
            if should_contain and {'id': page} not in refs: refs.append({'id': page})
            if not should_contain: refs[:] = [item for item in refs if item['id'] != page]
    parents.update_page, actions.update_page = parent_write, child_write
    old_request = parents._request
    parents._request = lambda method, path: SimpleNamespace(status_code=200, ok=True, json=lambda: {
        'properties': {parent_field: {'type': 'relation', 'relation': {'type': 'dual_property',
            'database_id': get_schema('action').notion_database_id,
            'dual_property': {'synced_property_name': child_field}}}}}) if '/databases/' in path else old_request(method, path)
    store = SQLiteIdMappingStore(); store.upsert(IdMapping('target', 'client_master'))
    gateway = NotionMergeGateway(clients, store)
    snapshot = gateway.snapshot('client_master', 'source', 'target')
    journal = Journal(gateway.plan(snapshot, {}))
    journal.job.update(dbKey='client_master', sourceId='source', targetId='target', snapshot=snapshot)
    MergeService(journal, gateway, lambda job: nullcontext()).execute('op', 'manager')
    assert journal.job['state'] == 'done' and source['archived']
    assert source['properties'][parent_field]['relation'] == []
    assert target['properties'][parent_field]['relation'] == [{'id': 'child'}]
    assert child['properties'][child_field]['relation'] == [{'id': 'target'}]


def test_body_post_timeout_recovers_by_exact_readback_without_second_append():
    import pytest
    from src.record_merge.domain import MergeHeld
    source = raw('client_master', 'source', {'取引先名': '同名施設'})
    target = raw('client_master', 'target', {'取引先名': '同名施設'})
    clients = {schema.key: Client(schema.key, {}) for schema in ALL_SCHEMAS}
    client = clients['client_master']; client.pages = {'source': source, 'target': target}
    def block(text): return {'type': 'paragraph', 'paragraph': {'rich_text': [{'type': 'text', 'text': {'content': text}}]}}
    bodies = {'source': [block('元の本文')], 'target': [block('先の本文')]}
    calls = []
    def request(method, path, **kwargs):
        if '/databases/' in path:
            return SimpleNamespace(status_code=200, ok=True, json=lambda: {'properties': {}})
        page = path.split('/')[2]
        if method == 'PATCH':
            calls.append(page)
            bodies[page].extend(deepcopy(kwargs['json_body']['children']))
            raise TimeoutError('private')
        return SimpleNamespace(status_code=200, ok=True, json=lambda: {'results': deepcopy(bodies[page]), 'has_more': False})
    client._request = request
    store = SQLiteIdMappingStore(); store.upsert(IdMapping('target', 'client_master'))
    gateway = NotionMergeGateway(clients, store)
    snapshot = gateway.snapshot('client_master', 'source', 'target')
    journal = Journal(gateway.plan(snapshot, {})); journal.job.update(dbKey='client_master', sourceId='source', targetId='target', snapshot=snapshot)
    service = MergeService(journal, gateway, lambda job: nullcontext())
    with pytest.raises(MergeHeld): service.execute('op', 'manager')
    assert not source['archived'] and journal.states[1] == 'reserved' and len(bodies['target']) == 2
    # 第三者編集があれば元を保持する。本文を戻せば実値確認で再開できる。
    original = deepcopy(bodies['target'][0])
    bodies['target'][0] = block('第三者の変更')
    with pytest.raises(MergeHeld, match='二重追加'): service.execute('op', 'manager')
    assert not source['archived'] and calls == ['target']
    bodies['target'][0] = original
    service.execute('op', 'manager')
    assert calls == ['target'] and source['archived'] and journal.aliases


def test_archived_source_with_unreadable_body_finishes_only_with_saved_progress():
    import pytest
    from src.record_merge.domain import MergeHeld
    source = raw('client_master', 'source', {'取引先名': '合成施設'})
    target = raw('client_master', 'target', {'取引先名': '合成施設'})
    clients = {schema.key: Client(schema.key, {}) for schema in ALL_SCHEMAS}
    client = clients['client_master']; client.pages = {'source': source, 'target': target}
    original_request = client._request
    def request(method, path):
        if path.startswith('/blocks/source/') and source['archived']:
            raise AssertionError('アーカイブ後の原本本文は取得できない')
        return original_request(method, path)
    client._request = request
    store = SQLiteIdMappingStore(); store.upsert(IdMapping('target', 'client_master'))
    gateway = NotionMergeGateway(clients, store)
    snapshot = gateway.snapshot('client_master', 'source', 'target')
    journal = Journal(gateway.plan(snapshot, {}))
    journal.job.update(dbKey='client_master', sourceId='source', targetId='target', snapshot=snapshot)
    assert MergeService(journal, gateway, lambda job: nullcontext()).execute('op', 'manager')['state'] == 'done'
    gateway.verify_archive_ready(journal.job)
    # 履歴がない外部アーカイブを、今回の成功として回収しない。
    unproven = deepcopy(journal.job); unproven['progress'] = {}
    with pytest.raises(MergeHeld, match='アーカイブ前'):
        gateway.verify_identity(unproven)
    # 統合先への第三者変更は、原本がアーカイブ済みでも検出する。
    client.update_page('target', {'取引先名': '第三者変更'})
    with pytest.raises(MergeHeld, match='統合後の値'):
        gateway.verify_archive_ready(journal.job)
