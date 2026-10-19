"""下書き訂正の固定境界と、結果不明からの安全な再開を検証する。"""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import correct_gmail_draft_activities as ops
from src.email_review.domain import identity
from src.record_merge.domain import MergeHeld
from tests.record_merge.test_postgres import local


def plan():
    rows, evidence = [], []
    for i in range(5):
        message = {'id': f'g{i}', 'thread_id': 'thread', 'label_ids': ['SENT'] if i == 4 else None,
                   'message_id_header': f'<m{i}@example.test>', 'from_header': 'owner@example.test'}
        row = {'id': f'r{i}', 'owner': 'owner@example.test', 'message': message,
               'canonical_key': identity(message, 'owner@example.test'), 'direction': 'outbound',
               'action_id': f'a{i}', 'contact_id': 'contact', 'revision': 2, 'state': 'done',
               'delivery_details': {'contact': 'delivered'}}
        row['correction_hold'] = {'key':'fixture', 'active':True, 'blocked_action_ids':[f'a{i}']}
        rows.append(row)
        events = [{'kind': 'messagesAdded', 'historyId': str(i * 3 + 1),
                   'messageId': f'g{i}', 'threadId': 'thread', 'messageLabels': ['SENT'] if i == 4 else ['DRAFT']}]
        if i != 4:
            events.append({**events[0], 'kind': 'messagesDeleted', 'historyId': str(i * 3 + 2)})
        evidence.append({'review_id': f'r{i}', 'gmail_id': f'g{i}', 'thread_id': 'thread', 'events': events})
    result = {'kind': ops.KIND, 'reviews': rows, 'evidence': evidence,
              'sentReviewId': 'r4', 'canonicalReviewId': 'r2', 'canonicalPage': 'a2'}
    result['planHash'] = ops.plan_hash(result)
    return result


def validate(p):
    return ops.validate_evidence(p['reviews'], p['evidence'], p['sentReviewId'], p['canonicalReviewId'])


def test_history_exact_chain():
    assert len(validate(plan())) == 5


@pytest.mark.parametrize('mutation', [
    lambda p: p['evidence'][0]['events'][0].update(messageId='other'),
    lambda p: p['evidence'][0]['events'][0].update(threadId='other'),
    lambda p: p['evidence'][0].update(gmail_id='other'),
    lambda p: p['evidence'][0]['events'][0].update(messageLabels=['SENT']),
    lambda p: p['evidence'][0]['events'][1].update(historyId='20'),
    lambda p: p['evidence'].append(deepcopy(p['evidence'][0])),
    lambda p: p['reviews'][4]['message'].update(label_ids=None),
    lambda p: p['reviews'][1].update(owner='other@example.test'),
    lambda p: p['reviews'][0].update(canonical_key='wrong'),
])
def test_unproven_identity_or_chain_is_held(mutation):
    p = plan(); mutation(p)
    with pytest.raises(MergeHeld): validate(p)


class MemoryJournal:
    def __init__(self): self.states = {}
    def state(self, stage): return self.states.get(stage)
    def reserve(self, stage): self.states.setdefault(stage, 'reserved')
    def done(self, stage): self.states[stage] = 'done'


def test_resume_rechecks_done_and_continues_unknown_stage():
    p, journal = plan(), MemoryJournal()
    io = Mock(); io.locks.side_effect = lambda _: nullcontext()
    count = 0
    def apply(stage, _):
        nonlocal count
        if stage == 'canonical' and count == 0:
            count += 1
            raise TimeoutError()
    io.apply.side_effect = apply
    with pytest.raises(TimeoutError): ops.run(p, io, journal)
    assert journal.state('merge') == 'done'
    assert journal.state('canonical') == 'reserved'
    io.reset_mock()
    assert ops.run(p, io, journal)['state'] == 'done'
    assert [call.args[0] for call in io.apply.call_args_list] == ['canonical', 'retire', 'handoff', 'email_logs']
    io.verify.assert_any_call('freeze', p)


def test_plan_tamper_never_locks_or_writes():
    p = plan(); p['canonicalPage'] = 'other'
    io = Mock()
    with pytest.raises(MergeHeld): ops.run(p, io, MemoryJournal())
    io.locks.assert_not_called()


def test_unknown_relations_must_remain_empty(monkeypatch):
    raw = {'properties': {'管理外': {'type': 'relation', 'relation': [], 'has_more': False}}}
    client = Mock(); client.get_raw_page.return_value = raw
    gateway = ops.FixedGateway({'action': client}, Mock(), ['page'], {'page': ['管理外']})
    assert gateway.verify_empty() == {'page': ['管理外']}
    raw['properties']['管理外']['relation'] = [{'id': 'new-child'}]
    with pytest.raises(MergeHeld): gateway.verify_empty()


def test_handoff_retains_sent_proof_and_releases_unique_canonical_action():
    io = object.__new__(ops.LiveIO)
    p = plan()
    rows = io.expected_rows(p, True)
    assert rows['r4']['action_id'] == 'a2' and rows['r4']['state'] == 'done'
    assert rows['r2']['action_id'] is None
    assert all(rows[f'r{i}']['state'] == 'excluded' for i in range(4))
    assert rows['r2']['original_action_id'] == 'a2'
    assert rows['r4']['message']['label_ids'] == ['SENT']
    assert all(rows[f'r{i}']['message']['label_ids'] == ['DRAFT'] for i in range(4))


def test_external_timeout_after_delete_recovers_without_second_delete(monkeypatch):
    item = {'tool': 'kintone', 'id': '42', 'page': 'old', 'before': {'$revision': '3'},
            'update': None, 'canonical': False, 'extra': {}}
    p = plan(); p['external'] = [item]
    journal = MemoryJournal()
    monkeypatch.setattr(ops, 'Journal', lambda *_: journal)
    io = object.__new__(ops.LiveIO); io.actor = 'manager'
    actual = deepcopy(item['before'])
    io.native = Mock(side_effect=lambda *_: actual)
    io.extras = Mock(return_value={})
    def write(_):
        nonlocal actual
        actual = None
        raise TimeoutError()
    io.write_external = Mock(side_effect=write)
    with pytest.raises(TimeoutError): io.external_change(p, False)
    assert journal.state('external:kintone:42') == 'reserved'
    # 実処理の再読分岐でDELETEせず既に取消済みと判定する。
    io.write_external = lambda item: ops.LiveIO.write_external(io, item)
    io.external_change(p, False)
    assert journal.state('external:kintone:42') == 'done'
    io.external_change(p, False)


def test_disappearance_before_reservation_is_not_our_success(monkeypatch):
    p = plan(); p['external'] = [{'tool': 'zoho', 'id': '42', 'page': 'old', 'before': {'id': '42'},
                                'canonical': False, 'update': None, 'extra': {}}]
    journal = MemoryJournal(); monkeypatch.setattr(ops, 'Journal', lambda *_: journal)
    io = object.__new__(ops.LiveIO); io.actor = 'manager'; io.native = Mock(return_value=None)
    io.write_external = Mock()
    with pytest.raises(MergeHeld): io.external_change(p, False)
    assert not journal.states
    io.write_external.assert_not_called()


def test_durable_stage_backup_and_idempotent_reservation(local):
    p = plan(); journal = ops.Journal(p, 'manager')
    journal.reserve('freeze'); journal.reserve('freeze')
    assert journal.state('freeze') == 'reserved'
    with ops.connect() as conn:
        row = conn.execute('SELECT * FROM "SyncOperationHistory" WHERE id=%s', (journal.identifier('freeze'),)).fetchone()
        assert row['beforeState']['plan'] == p
    journal.done('freeze')
    assert ops.Journal(p, 'manager').state('freeze') == 'done'


def test_general_resume_cannot_execute_fixed_workflow():
    from src.record_merge.operations import MergeOperations
    instance = object.__new__(MergeOperations)
    instance.authorize = Mock()
    instance.journal = Mock()
    instance.journal.get_authorized.return_value = {'snapshot': {'workflow': ops.KIND}}
    with pytest.raises(MergeHeld, match='専用スクリプト'): instance.resume('manager', 'job')


@pytest.fixture
def sql_plan(local):
    from pathlib import Path
    from psycopg.types.json import Jsonb
    p = plan()
    with ops.connect() as conn:
        conn.execute('CREATE TABLE "ClientNameIndex" ("normalizedName" text)')
        conn.execute(Path('dashboard/prisma/migrations/20261016090000_email_review/migration.sql').read_text())
        conn.execute('CREATE TABLE "EmailLog" (id text PRIMARY KEY, "gmailMessageId" text UNIQUE, body text, "digestedAt" timestamptz)')
        conn.execute('CREATE TABLE "EmailReminderLog" (id text PRIMARY KEY,"emailLogId" text REFERENCES "EmailLog"(id))')
        for row in p['reviews']:
            conn.execute('INSERT INTO "EmailReview" (id,data) VALUES (%s,%s)', (row['id'], Jsonb(row)))
            conn.execute('INSERT INTO "EmailLog" (id,"gmailMessageId",body) VALUES (%s,%s,%s)', (row['id'], row['message']['id'], '保持本文'))
        p['emailLogs'] = conn.execute('SELECT * FROM "EmailLog" WHERE id<>\'r4\' ORDER BY id').fetchall()
        p['sentEmailLog'] = conn.execute('SELECT * FROM "EmailLog" WHERE id=\'r4\'').fetchone()
    p['planHash'] = ops.plan_hash(p)
    return p


def test_sql_unique_action_handoff_and_repeat_preserves_all_audit(sql_plan):
    p = sql_plan
    io = object.__new__(ops.LiveIO); io.actor = 'manager'
    io.review_change(p)
    io.review_change(p)
    io.review_change(p, True)
    io.review_change(p, True)
    actual = io.rows([r['id'] for r in p['reviews']])
    assert actual == io.expected_rows(p, True)
    with ops.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM "EmailReviewHistory"').fetchone()['n'] == 10
        assert conn.execute('SELECT id FROM "EmailReview" WHERE data->>\'action_id\'=\'a2\'').fetchone()['id'] == 'r4'
    # 完了済み各段階も読み直し、再実行で受付や監査を増やさない。
    journal = ops.Journal(p, 'manager')
    for stage in ['freeze', 'merge', 'canonical', 'retire', 'handoff']:
        journal.reserve(stage); journal.done(stage)
    io.locks = lambda _: nullcontext()
    io.verify_identity = lambda _: None
    actual_verify = io.verify
    io.verify = lambda stage, p: actual_verify(stage, p) if stage in {'freeze','handoff','email_logs'} else None
    io.verify_final = lambda p: (io.verify('handoff',p), io.verify('email_logs',p))
    assert ops.run(p, io, journal)['state'] == 'done'
    assert ops.run(p, io, journal)['state'] == 'done'
    with ops.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM "EmailReviewHistory"').fetchone()['n'] == 15
        assert conn.execute('SELECT * FROM "EmailLog"').fetchall() == [p['sentEmailLog']]


def test_sql_reminder_or_edited_draft_prevents_deletion(sql_plan):
    p = sql_plan
    io = object.__new__(ops.LiveIO); io.actor = 'manager'
    with ops.connect() as conn:
        conn.execute('INSERT INTO "EmailReminderLog" VALUES (\'new\',\'r1\')')
    with pytest.raises(MergeHeld, match='リマインド'): io.remove_draft_logs(p)
    with ops.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM "EmailLog"').fetchone()['n'] == 5
        conn.execute('DELETE FROM "EmailReminderLog"')
        conn.execute('UPDATE "EmailLog" SET body=\'後から変更\' WHERE id=\'r1\'')
    with pytest.raises(MergeHeld, match='変わりました'): io.remove_draft_logs(p)
    with ops.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM "EmailLog"').fetchone()['n'] == 5


@pytest.mark.parametrize('tool', ['kintone', 'zoho', 'spreadsheet'])
def test_canonical_update_uses_version_or_fixed_row_and_zoho_no_notification(tool):
    from src.db_schema.base import Tool
    before = {'id': '42', '$revision': '9', 'Modified_Time': '2026-10-19T01:00:00+09:00', 'body': 'old'}
    item = {'tool': tool, 'id': '42', 'page': 'page', 'before': before, 'extra': {}, 'update': {'body': 'final'}}
    target = Mock(); target._app = 'app'; target._module = 'module'; target._sheet = 'sheet'
    target._client._request.return_value.status_code = 200
    target._client._request.return_value.json.return_value = {'data': [{'code': 'SUCCESS', 'details': {'id': '42'}}]}
    io = object.__new__(ops.LiveIO); io.native = Mock(return_value=before); io.extras = Mock(return_value={})
    wrapper = Mock(); wrapper._resolve.return_value = target
    io.targets = {Tool(tool): wrapper}
    io.write_external(item)
    if tool == 'kintone':
        target._client.update_record.assert_called_once_with('app','42',{'body':'final'},expected_version='9')
    elif tool == 'zoho':
        kwargs = target._client._request.call_args.kwargs
        assert kwargs['json_body']['trigger'] == []
        assert kwargs['extra_headers']['If-Unmodified-Since'] == before['Modified_Time']
        assert kwargs['idempotent'] is False
    else:
        target._client.update_row.assert_called_once_with('sheet',42,{'body':'final'})


def test_zoho_delete_explicitly_disables_workflow():
    from src.db_schema.base import Tool
    before = {'id': '42', 'Modified_Time': '2026-10-19T01:00:00+09:00'}
    target = Mock(); target._module = 'module'
    response = target._client._request.return_value
    response.status_code = 200; response.json.return_value = {'data':[{'code':'SUCCESS','details':{'id':'42'}}]}
    wrapper = Mock(); wrapper._resolve.return_value = target
    io = object.__new__(ops.LiveIO); io.targets = {Tool.ZOHO:wrapper}
    io.native = Mock(return_value=before); io.extras = Mock(return_value={})
    io.write_external({'tool':'zoho','id':'42','page':'page','before':before,'extra':{},'update':None})
    args = target._client._request.call_args
    assert args.args == ('DELETE','/module?ids=42&wf_trigger=false')
    assert args.kwargs['idempotent'] is False


@pytest.mark.parametrize('changed', [False, True])
def test_zoho_missing_get_timestamp_is_bound_to_same_coql_version(changed):
    from src.db_schema.base import Tool
    target = Mock(); target._client.get_record.return_value = {'id': '42', 'body': 'value'}
    wrapper = Mock(); wrapper._resolve.return_value = target
    io = object.__new__(ops.LiveIO); io.targets = {Tool.ZOHO: wrapper}
    io.zoho_version = Mock(side_effect=[{'id':'42','Modified_Time':'time1'},
                                      {'id':'42','Modified_Time':'time2' if changed else 'time1'}])
    if changed:
        with pytest.raises(MergeHeld, match='版が変わりました'): io.native('zoho','42','page')
    else:
        assert io.native('zoho','42','page') == {'id':'42','body':'value','Modified_Time':'time1'}


def test_all_parent_children_are_locked_without_nested_relocking(monkeypatch):
    p = plan(); p['merges'] = [{'snapshot': {'children': [{'dbKey':'client_master','id':'parent'}]}}]
    capture = Mock(return_value=nullcontext())
    monkeypatch.setattr(ops,'acquire_record_sync_locks',capture)
    io = object.__new__(ops.LiveIO); io.store = object()
    io.locks(p)
    keys = capture.call_args.args[1]
    assert {('action',f'a{i}') for i in range(5)} <= keys
    assert ('contact','contact') in keys and ('client_master','parent') in keys


@pytest.mark.parametrize('fail_append_number', [2, 3])
def test_fixed_five_build_merge_resume_final_with_shared_parent(sql_plan, monkeypatch, fail_append_number):
    """外部I/Oだけを代替し、計画・4統合・SQL履歴・別名・最終照合を通す。"""
    from src.db_schema.base import Tool, PropertyType
    from src.db_schema.registry import ALL_SCHEMAS, get_schema
    from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
    from src.record_merge.aliases import resolve_alias
    from src.sync_engine.clients.spreadsheet_client import _to_cell_value
    from tests.record_merge.test_gateway import Client, raw
    from psycopg.types.json import Jsonb

    p = sql_plan
    title = next(prop.name for prop in get_schema('action').properties if prop.property_type == PropertyType.TITLE)
    parent_field = next(prop.name for prop in get_schema('client_master').properties if prop.relation_target == 'action')
    child_field = '👨‍👩‍👧‍👦 取引先マスター'
    clients = {schema.key: Client(schema.key, {}) for schema in ALL_SCHEMAS}
    actions, parents = clients['action'], clients['client_master']
    parents.pages['parent'] = raw('client_master', 'parent', {'取引先名':'合成施設', parent_field:[f'a{i}' for i in range(5)]})
    blocks = {}
    for i, row in enumerate(p['reviews']):
        values = {title:f'活動{i}', '履歴メモ':f'本文{i}', 'アクション日':'2026-10-19T01:00:00+00:00',
                  child_field:['parent'], 'アクション種別':'メール'}
        actions.pages[f'a{i}'] = raw('action',f'a{i}',values)
        actions.pages[f'a{i}']['properties']['管理外'] = {'type':'relation','relation':[],'has_more':False}
        row['written_properties'] = values
        row.pop('correction_hold', None)
        blocks[f'a{i}'] = [{'type':'paragraph','paragraph':{'rich_text':[{'type':'text','text':{'content':f'追跡{i}'}}]}}]
    with ops.connect() as conn:
        for row in p['reviews']:
            conn.execute('UPDATE "EmailReview" SET data=%s WHERE id=%s',(Jsonb(row),row['id']))
    for client in clients.values():
        client.get_raw_page = lambda page, client=client: deepcopy(client.pages[page])

    append_calls = []
    def response(data): return SimpleNamespace(status_code=200, ok=True, json=lambda:deepcopy(data))
    def action_request(method,path,**kwargs):
        if '/databases/' in path:
            return response({'properties':{child_field:{'type':'relation','relation':{
                'type':'dual_property','database_id':get_schema('client_master').notion_database_id,
                'dual_property':{'synced_property_name':parent_field}}}}})
        page = path.split('/')[2]
        if method == 'PATCH':
            additions = deepcopy(kwargs['json_body']['children'])
            append_calls.append(page)
            blocks[page].extend(additions)
            if len(append_calls) == fail_append_number:
                raise TimeoutError('合成通信断: 本文追加直後')
            return response({'results':[{'id':f'block-{len(append_calls)}-{i}'} for i in range(len(additions))]})
        return response({'results':blocks[page],'has_more':False})
    actions._request = action_request
    original_parent_write = parents.update_page
    original_action_write = actions.update_page
    def parent_write(page, values):
        original_parent_write(page, values)
        if parent_field in values:
            for aid, action in actions.pages.items():
                refs = action['properties'][child_field]['relation']
                if aid not in values[parent_field]: refs[:] = [r for r in refs if r['id'] != page]
                elif {'id':page} not in refs: refs.append({'id':page})
    def action_write(page, values):
        original_action_write(page, values)
        if child_field in values:
            refs = parents.pages['parent']['properties'][parent_field]['relation']
            if 'parent' in values[child_field] and {'id':page} not in refs: refs.append({'id':page})
            if 'parent' not in values[child_field]: refs[:] = [r for r in refs if r['id'] != page]
    parents.update_page, actions.update_page = parent_write, action_write

    class Store(SQLiteIdMappingStore):
        def get(self, key):
            return super().get(resolve_alias('action','notion',key) or key)
    store = Store()
    native = {}
    for i in range(4):
        store.upsert(IdMapping(f'a{i}','action',zoho_id=str(100+i),kintone_id=str(200+i),spreadsheet_row=i+1))
        row = p['reviews'][i]
        native['kintone',str(200+i)] = {'$id':str(200+i),'$revision':'1','comment':f'作成者\n本文{i}'}
        native['zoho',str(100+i)] = {'id':str(100+i),'Modified_Time':'2026-10-19T01:00:00Z',
                                    **{k:row['written_properties'][k] for k in (title,'履歴メモ','アクション日')}}
        native['spreadsheet',str(i+1)] = {'同期キー':f'a{i}','削除フラグ':False,
            **{k:_to_cell_value(row['written_properties'][k],field_name=k) for k in (title,'履歴メモ','アクション日')}}
    zoho = SimpleNamespace(_to_zoho_payload=lambda changes,key:deepcopy(changes))
    wrapper = SimpleNamespace(_resolve=lambda key:zoho)
    io = object.__new__(ops.LiveIO)
    io.actor, io.store, io.clients, io.targets = 'manager',store,clients,{Tool.ZOHO:wrapper}
    io.native = lambda tool,identifier,page:deepcopy(native.get((tool,identifier)))
    io.extras = lambda tool,identifier:{}
    def external_write(item):
        native[item['tool'],item['id']] = io.desired_external(item)
    io.write_external = external_write
    monkeypatch.setattr(ops,'acquire_record_sync_locks',lambda *_:nullcontext())
    spec = {k:p[k] for k in ('evidence','sentReviewId','canonicalReviewId')}
    spec['reviewIds'] = [r['id'] for r in p['reviews']]
    assert io.reserve(spec)['state'] == 'reserved'
    assert io.reserve(spec)['state'] == 'reserved'
    from src.email_review import repository, domain, service as review_service
    from src.sync_engine.production_wiring import SkipTrackingDispatcher
    from src.sync_engine.sync_event import SyncEvent
    from datetime import datetime, timezone
    monkeypatch.setattr(repository, '_connect', ops.connect)
    assert all(repository.action_registration_blocked(f'a{i}') for i in range(5))
    held_sent = io.rows(['r4'])['r4']
    for action in ['confirm','hold','exclude','resume','reconcile']:
        with pytest.raises(domain.Conflict, match='訂正処理中'):
            domain.decide(held_sent,revision=held_sent['revision'],action=action,selection={},actor='manager',channel='test')
    repo_mock, gateway_mock = Mock(), Mock()
    service = review_service.Service(repo_mock, gateway_mock)
    for method in (service.evaluate, service.process, service.reconcile):
        assert method(held_sent) == held_sent
    assert not repo_mock.mock_calls and not gateway_mock.mock_calls
    underlying, creation = Mock(), Mock()
    event = SyncEvent(Tool.NOTION,'action','a4',datetime.now(timezone.utc))
    result = SkipTrackingDispatcher(underlying,creation_service=creation).dispatch(event)
    assert result.skipped and not underlying.mock_calls and not creation.mock_calls
    built = io.build(spec)
    assert len(built['merges']) == 4 and len(built['desiredBlocks']) == 5
    # 後続の親関連配列は、それまでの統合で除かれた旧IDを含まない。
    prior = []
    for item in built['merges']:
        child = item['snapshot']['children'][0]
        assert all(identifier not in child['before'] for identifier in prior)
        prior.append(item['snapshot']['sourceId'])
    journal = ops.Journal(built,'manager')
    with pytest.raises(MergeHeld,match='外部処理'):
        ops.run(built,io,journal)
    assert len(append_calls) == fail_append_number
    assert ops.run(built,io,journal)['state'] == 'done'
    assert ops.run(built,io,journal)['state'] == 'done'
    assert len(append_calls) == 4 and len(blocks['a2']) == 5
    assert parents.pages['parent']['properties'][parent_field]['relation'] == [{'id':'a2'}]
    assert all(resolve_alias('action','notion',f'a{i}') == 'a2' for i in (0,1,3,4))
    assert all(actions.pages[f'a{i}']['archived'] for i in (0,1,3,4))
    assert not actions.pages['a2']['archived']
    assert not repository.action_registration_blocked('a2')
    assert all(repository.action_registration_blocked(f'a{i}') for i in (0,1,3,4))
    assert io.rows(['r4'])['r4']['correction_hold']['active'] is False
    # 旧Sheet行の取消通知は旧同期キーのまま。正本へ転送して削除しない。
    from src.sync_engine.dispatcher import Dispatcher
    monkeypatch.setattr('src.sync_operations.exclusions.is_excluded', lambda *_:False)
    dispatcher = Dispatcher(store,{})
    old_event = SyncEvent(Tool.SPREADSHEET,'action','1',datetime.now(timezone.utc),
                         properties={'削除フラグ':True},source_notion_key='a0')
    assert dispatcher.dispatch(old_event).reason == 'merged_source_archived'
    assert not actions.pages['a2']['archived']
    assert actions.pages['a2']['properties']['履歴メモ']['rich_text'][0]['text']['content'] == '本文4'
    with ops.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM "RecordMergeJob" WHERE state=\'done\'').fetchone()['n'] == 4
        assert conn.execute('SELECT count(*) AS n FROM "RecordMergeAlias" WHERE tool=\'notion\'').fetchone()['n'] == 4
        assert conn.execute('SELECT id FROM "EmailReview" WHERE data->>\'action_id\'=\'a2\'').fetchone()['id'] == 'r4'
        assert conn.execute('SELECT * FROM "EmailLog"').fetchall() == [built['sentEmailLog']]


def test_creation_rechecks_reservation_inside_page_lock(monkeypatch):
    from tests.hub_creation.test_action_identity import action_setup, event
    service, journal, notion, adapter, pages = action_setup()
    guard = Mock(side_effect=[False, True])
    monkeypatch.setattr('src.email_review.repository.action_registration_blocked', guard)
    assert service.handle(event()) == 'email_sent_evidence_required'
    assert guard.call_count == 2 and adapter.creates == 0 and not journal.claims


def test_digest_notification_changes_do_not_block_but_are_preserved(sql_plan):
    p = sql_plan
    io = object.__new__(ops.LiveIO); io.actor = 'manager'
    with ops.connect() as conn:
        conn.execute('UPDATE "EmailLog" SET "digestedAt"=now()')
    io.remove_draft_logs(p)
    io.verify('email_logs',p)
    with ops.connect() as conn:
        observed = conn.execute('SELECT "beforeState" FROM "SyncOperationHistory" WHERE id=%s',
            (ops.Journal(p,'manager').identifier('email_logs_observed'),)).fetchone()['beforeState']['rows']
        assert len(observed) == 4 and all(r['digestedAt'] for r in observed)
        assert conn.execute('SELECT "digestedAt" FROM "EmailLog" WHERE id=\'r4\'').fetchone()['digestedAt']
    assert ops.LiveIO.log_evidence({'contactPageId':'fixed','digestedAt':'now'}) == {'contactPageId':'fixed'}


def test_old_unreserved_plan_cannot_apply():
    p = plan()
    for row in p['reviews']: row.pop('correction_hold')
    p['planHash'] = ops.plan_hash(p)
    io = Mock()
    with pytest.raises(MergeHeld,match='訂正予約前'): ops.run(p,io,MemoryJournal())
    io.locks.assert_not_called()


def test_reservation_does_not_interrupt_running_worker_or_partially_hold(sql_plan, monkeypatch):
    from psycopg.types.json import Jsonb
    p = sql_plan
    rows = deepcopy(p['reviews'])
    for row in rows: row.pop('correction_hold')
    rows[-1]['state'] = 'processing'
    with ops.connect() as conn:
        for row in rows:
            conn.execute('UPDATE "EmailReview" SET data=%s WHERE id=%s',(Jsonb(row),row['id']))
    io = object.__new__(ops.LiveIO); io.actor = 'manager'; io.store = Mock()
    monkeypatch.setattr(ops,'acquire_record_sync_locks',lambda *_:nullcontext())
    spec = {k:p[k] for k in ('evidence','sentReviewId','canonicalReviewId')}
    spec['reviewIds'] = [r['id'] for r in rows]
    with pytest.raises(MergeHeld,match='処理中'): io.reserve(spec)
    assert io.rows(spec['reviewIds']) == {r['id']:r for r in rows}
    with ops.connect() as conn:
        assert conn.execute('SELECT count(*) AS n FROM "EmailReviewHistory"').fetchone()['n'] == 0


def test_correction_page_lookup_has_bitmap_indexes_for_both_branches(sql_plan):
    from pathlib import Path
    import json
    with ops.connect() as conn:
        conn.execute(Path('dashboard/prisma/migrations/20261019030000_email_review_correction_guard/migration.sql').read_text())
        # 小さな合成表では全走査の方が安い。利用可能な索引経路を明示して確認する。
        conn.execute('SET LOCAL enable_seqscan=off')
        result = conn.execute('''EXPLAIN (FORMAT JSON) SELECT data FROM "EmailReview"
            WHERE data->>'action_id'=ANY(%s)
            OR (data->'correction_hold'->'blocked_action_ids') ?| %s''', (['a4'],['a4'])).fetchone()
    explain = json.dumps(result,default=str)
    assert 'EmailReview_action_idx' in explain and 'EmailReview_correction_pages_idx' in explain
    assert 'BitmapOr' in explain and 'Seq Scan' not in explain
