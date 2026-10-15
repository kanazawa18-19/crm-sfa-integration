"""タイトルを共有する架電履歴と、同じ接触内容の二重登録を区別する。"""
from types import SimpleNamespace
import pytest
from src.hub_creation.action_identity import CLIENT, fingerprint, zoho_matcher
from src.hub_creation.domain import CreationHeld
from src.hub_creation.duplicate_scan import scan_input_hash
from src.record_merge.domain import digest
from src.hub_creation.adapters import ZohoCreationAdapter
from tests.hub_creation.test_service import setup, NOW
from src.db_schema.registry import get_schema
from src.db_schema.base import Tool
from src.sync_engine.sync_event import SyncEvent

COMPANY = '11111111-1111-1111-1111-111111111111'
OTHER = '22222222-2222-2222-2222-222222222222'
TITLE = '商談回数・電話回数・メール回数（何回目）'


def properties():
    return {TITLE: '【電話】1回目', CLIENT: [COMPANY], 'アクション種別': 'テレアポ',
            'アクション日': '2026-10-13', '履歴メモ': '担当者不在', '先方担当者': '担当者', '案件名': []}


def raw(page, props):
    result = {}
    for name, value in props.items():
        if name in (CLIENT, '案件名'):
            result[name] = {'type': 'relation', 'relation': [{'id': v} for v in value]}
        elif name == 'アクション日':
            result[name] = {'type': 'date', 'date': {'start': value}}
        elif name == 'アクション種別':
            result[name] = {'type': 'select', 'select': {'name': value}}
        else:
            kind = 'title' if name == TITLE else 'rich_text'
            result[name] = {'type': kind, kind: [{'plain_text': value}]}
    return {'id': page, 'created_time': '2026-10-13T00:00:00Z',
            'parent': {'database_id': get_schema('action').notion_database_id}, 'properties': result}


def action_setup():
    service, _, journal, notion, adapter, store = setup()
    pages = {'page': raw('page', properties())}
    notion.get_raw_page = lambda key: pages[key]
    notion.query_raw = lambda body: {'results': list(pages.values()), 'has_more': False}
    service.notion_clients = {'action': notion}
    adapter.create = lambda db, payload: create(adapter)
    return service, journal, notion, adapter, pages


def create(adapter):
    adapter.creates += 1
    if adapter.fail:
        raise TimeoutError('結果不明')
    return 'external-' + str(adapter.creates)


def event(page='page'):
    return SyncEvent(Tool.NOTION, 'action', page, NOW, properties={})


def test_title_changes_do_not_change_identity_but_actual_contact_changes_do():
    original = properties()
    for name, value in [(TITLE, '別タイトル'), ('案件名', None)]:
        changed = {**original, name: value}
        assert fingerprint(original) == fingerprint(changed)
    for name, value in [(CLIENT, [OTHER]), ('履歴メモ', '電話接続'), ('アクション日', '2026-10-14'),
                        ('先方担当者', '別担当者'), ('アクション種別', 'メール'), ('案件名', [OTHER])]:
        assert fingerprint(original) != fingerprint({**original, name: value})


@pytest.mark.parametrize('clients', [None, [], [COMPANY, OTHER], ['invalid']])
def test_unknown_or_multiple_clients_are_held(clients):
    with pytest.raises(CreationHeld):
        fingerprint({**properties(), CLIENT: clients})


def test_same_title_different_contacts_create_once_each_without_projects():
    service, journal, notion, adapter, pages = action_setup()
    pages['other'] = raw('other', {**properties(), '履歴メモ': '再度電話済み'})
    assert service.handle(event()) == 'hub_creation_complete'
    assert service.handle(event('other')) == 'hub_creation_complete'
    assert service.handle(event()) is None
    assert adapter.creates == 2


def test_different_title_same_contact_content_is_held():
    service, journal, notion, adapter, pages = action_setup()
    pages['other'] = raw('other', {**properties(), TITLE: '名称だけ変更'})
    assert service.handle(event()) == 'hub_creation_held'
    assert adapter.creates == 0


def test_unknown_client_does_not_create_external():
    service, journal, notion, adapter, pages = action_setup()
    pages['page'] = raw('page', {**properties(), CLIENT: []})
    assert service.handle(event()) == 'hub_creation_held'
    assert adapter.creates == 0


def test_reserved_original_never_posts_again_even_after_edit():
    service, journal, notion, adapter, pages = action_setup()
    adapter.fail = True
    service.handle(event())
    assert journal.get('notion:page', 'zoho')['state'] == 'reserved'
    pages['page'] = raw('page', {**properties(), '履歴メモ': '編集後'})
    service.handle(event())
    assert adapter.creates == 1


def test_content_reservation_blocks_another_source_when_notion_query_lags():
    service, journal, notion, adapter, pages = action_setup()
    notion.query_raw = lambda body: {'results': [], 'has_more': False}
    assert service.handle(event()) == 'hub_creation_complete'
    pages['other'] = raw('other', properties())
    assert service.handle(event('other')) == 'hub_creation_held'
    assert adapter.creates == 1


def payload():
    return {'Name': '【電話】1回目', 'field6': {'id': '123'}, 'field7': 'テレアポ',
            'field4': '2026-10-13', 'field': '担当者不在', 'field2': '担当者'}


def test_zoho_matcher_compares_content_not_title_and_rejects_incomplete_response():
    source = payload()
    fields, matches = zoho_matcher(source)
    assert 'Name' not in fields
    assert matches({**source, 'Name': '別名'})
    for key, value in [('field6', {'id': '456'}), ('field', '接続済み'), ('field4', '2026-10-14')]:
        assert not matches({**source, key: value})
    del source['field2']
    with pytest.raises(CreationHeld):
        matches(source)


def test_action_scan_invalidates_old_title_only_checkpoint():
    context = {'dbKey': 'action', 'sourceKey': 'notion:page', 'sourceProperties': properties()}
    assert scan_input_hash(context) != digest(context)
    context['dbKey'] = 'chain'
    assert scan_input_hash(context) == digest(context)


def test_zoho_action_list_duplicate_and_distinct_content(monkeypatch):
    adapter = ZohoCreationAdapter(SimpleNamespace())
    adapter._get = lambda path, **kw: {'data': [{'id': '1', **payload(), 'field': '別履歴'}], 'info': {'more_records': False}}
    adapter._check_action_duplicates('CustomModule2', payload())
    adapter._get = lambda path, **kw: {'data': [{'id': '1', **payload()}], 'info': {'more_records': False}}
    with pytest.raises(CreationHeld, match='一致'):
        adapter._check_action_duplicates('CustomModule2', payload())


def test_zoho_action_delegates_same_matcher_to_resumable_scan():
    calls = []
    scan = SimpleNamespace(exists=lambda: True, check=lambda *args: calls.append(args))
    adapter = ZohoCreationAdapter(SimpleNamespace(), duplicate_scan=scan)
    adapter._check_action_duplicates('CustomModule2', payload())
    db, module, fields, matches = calls[0]
    assert db == 'action' and 'field6' in fields and 'Name' not in fields
    assert matches(payload())
    assert not matches({**payload(), 'field6': {'id': '456'}})


def test_notion_candidates_paginate_and_hold_same_content_on_later_page():
    service, journal, notion, adapter, pages = action_setup()
    queries = []
    def query(body):
        queries.append(dict(body))
        if 'start_cursor' not in body:
            return {'results': [raw('different', {**properties(), '履歴メモ': '別内容'})],
                    'has_more': True, 'next_cursor': 'next'}
        return {'results': [raw('duplicate', properties())], 'has_more': False}
    notion.query_raw = query
    assert service.handle(event()) == 'hub_creation_held'
    assert queries[1]['start_cursor'] == 'next'
    assert adapter.creates == 0


def test_partial_notion_relations_do_not_claim_verified_identity():
    service, journal, notion, adapter, pages = action_setup()
    pages['page']['properties'][CLIENT]['has_more'] = True
    assert service.handle(event()) == 'hub_creation_held'
    assert adapter.creates == 0


@pytest.mark.parametrize('defer_first', [False, True])
def test_old_action_checkpoint_restarts_full_scan_with_consistent_defer_hash(defer_first):
    from tests.hub_creation.test_duplicate_scan import Journal, Client
    from src.hub_creation.duplicate_scan import ZohoDuplicateScan
    from src.record_merge.creation_candidates import candidate_context
    journal, client = Journal(), Client(0)
    context = {'dbKey': 'action', 'sourceId': 'page', 'sourceKey': 'notion:page', 'sourceProperties': properties()}
    journal.save(context, digest(context), {'phase': 'verified', 'last_id': '5000', 'since': '2026-10-01T00:00:00+00:00'})
    scan = ZohoDuplicateScan(client, journal)
    with candidate_context('action', 'page', 'notion:page', properties()):
        if defer_first:
            scan.defer()
            # 比較値が未確定のdeferでは進捗を維持し、直後のcheckで無効化する。
            assert journal.row['checkpoint']['phase'] == 'verified'
        fields, matches = zoho_matcher(payload())
        scan.check('action', 'CustomModule2', fields, matches)
        context['actionComparison'] = matches.comparison
    assert 'id > 0' in client.queries[0]
    assert 'Modified_Time' not in client.queries[0]
    assert journal.row['inputHash'] == scan_input_hash(context)


def test_sheet_same_content_parallel_sources_share_reservation_identity():
    service, journal, notion, adapter, pages = action_setup()
    notion.query_raw = lambda body: {'results': [], 'has_more': False}
    created = []
    def create_page(props):
        page = f'new-page-{len(created)}'
        created.append(page)
        pages[page] = raw(page, props)
        return page
    notion.create_page_once = create_page
    service.sheet_gateway = SimpleNamespace(read=lambda *a, **kw: (properties(), 1, 9), update=lambda *a, **kw: 9)
    first = SyncEvent(Tool.SPREADSHEET, 'action', '9', NOW, registration_key='new:first')
    second = SyncEvent(Tool.SPREADSHEET, 'action', '10', NOW, registration_key='new:second')
    assert service.handle(first) == 'hub_creation_complete'
    assert service.handle(second) == 'hub_creation_held'
    assert len(created) == 1 and adapter.creates == 1
    # 作成されたNotion側からの再通知も、元のSheets登録予約へ戻る。
    assert service.handle(event(created[0])) is None
    assert adapter.creates == 1


@pytest.mark.parametrize('field', [CLIENT, '案件名', 'アクション日', 'アクション種別', '履歴メモ', '先方担当者'])
@pytest.mark.parametrize('candidate', [False, True])
@pytest.mark.parametrize('mutation', ['missing', 'wrong_type', 'missing_value'])
def test_incomplete_identity_on_source_or_candidate_is_never_nonmatching(field, candidate, mutation):
    service, journal, notion, adapter, pages = action_setup()
    target = raw('candidate', properties()) if candidate else pages['page']
    if mutation == 'missing':
        del target['properties'][field]
    elif mutation == 'wrong_type':
        target['properties'][field]['type'] = 'number'
    else:
        del target['properties'][field][target['properties'][field]['type']]
    if candidate:
        pages['candidate'] = target
    assert service.handle(event()) == 'hub_creation_held'
    assert adapter.creates == 0


def test_timestamp_date_identity_and_zoho_comparison_use_same_calendar_day():
    from src.hub_creation.action_identity import action_day
    timestamp = '2026-10-13T10:00:00+09:00'
    assert fingerprint(properties()) == fingerprint({**properties(), 'アクション日': timestamp})
    fields, matches = zoho_matcher({**payload(), 'field4': timestamp})
    assert matches(payload())
    assert action_day(timestamp) == '2026-10-13'


@pytest.mark.parametrize('defer_first', [False, True])
def test_changed_external_mapping_restarts_full_action_scan_and_finds_old_record(defer_first):
    from tests.hub_creation.test_duplicate_scan import Journal, Client
    from src.hub_creation.duplicate_scan import ZohoDuplicateScan
    from src.record_merge.creation_candidates import candidate_context, creation_context
    journal, client = Journal(), Client(0)
    scan = ZohoDuplicateScan(client, journal)
    old_fields, old_matches = zoho_matcher(payload())
    with candidate_context('action', 'page', 'notion:page', properties()):
        scan.check('action', 'CustomModule2', old_fields, old_matches)
    old_hash = journal.row['inputHash']
    fields, matches = zoho_matcher({**payload(), 'field6': {'id': '456'}})
    queries = []
    def page(module, read_fields, checkpoint):
        queries.append(dict(checkpoint))
        if checkpoint['phase'] == 'full':
            return [{'id': '1', **payload(), 'field6': {'id': '456'}}], False
        return [], False
    scan._page = page
    with candidate_context('action', 'page', 'notion:page', properties()):
        if defer_first:
            # adapterが確定済みpayloadをコンテキストへ渡してから、時間切れのdeferへ入る。
            creation_context()['actionComparison'] = matches.comparison
            scan.defer()
        with pytest.raises(CreationHeld, match='一致'):
            scan.check('action', 'CustomModule2', fields, matches)
    assert queries[0]['phase'] == 'full' and queries[0]['last_id'] == '0'
    assert journal.row['inputHash'] != old_hash


@pytest.mark.parametrize('records', [[{'id': '1', **payload(), 'field': '別履歴'}] * 2, []])
def test_action_zoho_duplicate_ids_and_empty_more_page_are_held(records):
    adapter = ZohoCreationAdapter(SimpleNamespace())
    adapter._get = lambda _, **kw: {'data': records, 'info': {'more_records': True}}
    with pytest.raises(CreationHeld):
        adapter._check_action_duplicates('CustomModule2', payload())


def test_action_zoho_2000_boundary_passes_same_content_matcher_to_scan():
    calls, pages = [], []
    scan = SimpleNamespace(exists=lambda: False, check=lambda *args: calls.append(args))
    adapter = ZohoCreationAdapter(SimpleNamespace(), duplicate_scan=scan)
    def get(path, **kw):
        offset = len(pages) * 200
        pages.append(path)
        return {'data': [{'id': str(offset + i), **payload(), 'field': '別履歴'} for i in range(1, 201)],
                'info': {'more_records': True}}
    adapter._get = get
    adapter._check_action_duplicates('CustomModule2', payload())
    assert len(pages) == 10 and len(calls) == 1
    assert calls[0][3](payload())


def test_notion_cursor_cycle_holds_without_post():
    service, journal, notion, adapter, pages = action_setup()
    notion.query_raw = lambda _: {'results': [], 'has_more': True, 'next_cursor': 'repeated'}
    assert service.handle(event()) == 'hub_creation_held'
    assert adapter.creates == 0


@pytest.mark.parametrize('state', ['pending', 'held'])
def test_metadata_timeout_keeps_existing_action_scan_progress_and_hold(state):
    from copy import deepcopy
    from tests.hub_creation.test_duplicate_scan import Journal
    from src.hub_creation.duplicate_scan import ZohoDuplicateScan
    from src.record_merge.creation_candidates import candidate_context, creation_context
    from src.infrastructure.http_budget import HttpBudgetExceeded
    journal = Journal()
    scan = ZohoDuplicateScan(SimpleNamespace(), journal)
    adapter = ZohoCreationAdapter(SimpleNamespace(), duplicate_scan=scan)
    def timeout(db, props):
        raise HttpBudgetExceeded()
    adapter._plan = timeout
    with candidate_context('action', 'page', 'notion:page', properties()):
        creation_context()['actionComparison'] = zoho_matcher(payload())[1].comparison
        journal.save(creation_context(), scan_input_hash(creation_context()),
                     {'phase': 'full', 'last_id': '4000', 'since': '2026-10-01T00:00:00+00:00'},
                     state=state, reason='既存の保留理由')
        before = deepcopy(journal.row)
        saves = len(journal.saved)
        with pytest.raises(CreationHeld, match='分割処理中'):
            adapter.plan('action', properties())
        assert journal.row == before
        assert len(journal.saved) == saves


def test_first_metadata_timeout_initializes_resumable_action_scan():
    from tests.hub_creation.test_duplicate_scan import Journal
    from src.hub_creation.duplicate_scan import ZohoDuplicateScan
    from src.record_merge.creation_candidates import candidate_context
    from src.infrastructure.http_budget import HttpBudgetExceeded
    journal = Journal()
    scan = ZohoDuplicateScan(SimpleNamespace(), journal)
    adapter = ZohoCreationAdapter(SimpleNamespace(), duplicate_scan=scan)
    def timeout(db, props):
        raise HttpBudgetExceeded()
    adapter._plan = timeout
    with candidate_context('action', 'page', 'notion:page', properties()):
        with pytest.raises(CreationHeld, match='分割処理中'):
            adapter.plan('action', properties())
    assert journal.row['state'] == 'pending'
    assert journal.row['checkpoint']['phase'] == 'full'
    assert journal.row['checkpoint']['last_id'] == '0'


def test_external_reservation_ignores_project_while_notion_identity_keeps_it():
    left = {**properties(), '案件名': [COMPANY]}
    right = {**properties(), '案件名': [OTHER]}
    assert fingerprint(left) != fingerprint(right)
    assert fingerprint(left, external=True) == fingerprint(right, external=True)


@pytest.mark.parametrize('first_unknown', [False, True])
def test_different_projects_same_content_share_external_reservation(first_unknown):
    service, journal, notion, adapter, pages = action_setup()
    pages['page'] = raw('page', {**properties(), '案件名': [COMPANY]})
    pages['other'] = raw('other', {**properties(), '案件名': [OTHER]})
    # 候補照合は案件が異なるため通過し、外部一覧も未反映の条件を再現。
    adapter.fail = first_unknown
    service.handle(event())
    assert journal.get('notion:page', 'zoho')['state'] == ('reserved' if first_unknown else 'created')
    assert service.handle(event('other')) == 'hub_creation_held'
    assert adapter.creates == 1
    if first_unknown:
        pages['page'] = raw('page', {**properties(), '案件名': [OTHER], '履歴メモ': '入力を修正'})
        service.handle(event())
        assert journal.get('notion:page', 'zoho')['state'] == 'reserved'
        assert adapter.creates == 1


def test_action_list_http_204_first_page_means_zero_records():
    def request(method, path):
        return SimpleNamespace(status_code=204, ok=True,
                               json=lambda: pytest.fail('204の本文は読みません'))
    adapter = ZohoCreationAdapter(SimpleNamespace(_request=request))
    assert adapter._check_action_duplicates('CustomModule2', payload()) is None


def test_action_list_http_200_empty_object_still_holds():
    adapter = ZohoCreationAdapter(SimpleNamespace(_request=lambda *args:
        SimpleNamespace(status_code=200, ok=True, json=lambda: {})))
    with pytest.raises(CreationHeld, match='一覧照合結果'):
        adapter._check_action_duplicates('CustomModule2', payload())


def test_action_list_http_204_after_more_records_is_not_verified_empty():
    replies = [SimpleNamespace(status_code=200, ok=True, json=lambda: {
        'data': [{'id': '1', **payload(), 'field': '別履歴'}], 'info': {'more_records': True}}),
        SimpleNamespace(status_code=204, ok=True)]
    adapter = ZohoCreationAdapter(SimpleNamespace(_request=lambda *args: replies.pop(0)))
    with pytest.raises(CreationHeld, match='一覧照合結果'):
        adapter._check_action_duplicates('CustomModule2', payload())


@pytest.mark.parametrize('client', [None, '', [], {}, {'id': None}])
def test_action_matcher_missing_source_client_is_a_business_hold(client):
    with pytest.raises(CreationHeld, match='取引先のZoho ID'):
        zoho_matcher({**payload(), 'field6': client})
