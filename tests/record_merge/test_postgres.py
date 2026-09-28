"""統合履歴・旧IDの原子性を、明示したローカルDBだけで確認する。"""
from pathlib import Path
from urllib.parse import quote, urlparse
import os
import uuid
import psycopg
from psycopg import sql
import pytest
from src.record_merge.journal import MergeJournal, connect
from src.record_merge.aliases import resolve_alias
from src.record_merge.domain import MergeHeld
from src.sync_review.domain import ReviewForbidden


@pytest.fixture
def local(monkeypatch):
    dsn = os.environ.get('SYNC_REVIEW_TEST_DATABASE_URL')
    if not dsn: pytest.skip('明示したローカルDBのみ')
    assert urlparse(dsn).hostname in {'127.0.0.1', 'localhost'}
    assert urlparse(dsn).path == '/crm_field_review_test'
    schema = 'merge_' + uuid.uuid4().hex
    scoped = dsn + '?options=' + quote('-csearch_path=' + schema)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    monkeypatch.setenv('DATABASE_URL', scoped)
    monkeypatch.setenv('RECORD_MERGE_ENABLED', 'true')
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute('CREATE TABLE "User" (id TEXT PRIMARY KEY,"isManager" BOOLEAN NOT NULL)')
            conn.execute("INSERT INTO \"User\" VALUES ('manager',true),('viewer',false)")
            for migration in ('20260928060000_sync_operation_history', '20260928070000_record_merge'):
                conn.execute((Path('dashboard/prisma/migrations') / migration / 'migration.sql').read_text())
        yield MergeJournal()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def prepare(journal):
    snapshot = {'dbKey': 'project', 'sourceId': 'source', 'targetId': 'target', 'children': [],
                'aliases': {'notion': 'source', 'zoho': 'old-zoho'}}
    steps = [{'kind': 'archive', 'dbKey': 'project', 'id': 'source', 'before': False, 'desired': True}]
    job = journal.create(snapshot, steps, 'manager')
    return journal.decide(job['id'], 'manager', job['planHash'], 'approve')


def test_manager_only_and_all_steps_required_before_aliases(local):
    job = prepare(local)
    with pytest.raises(ReviewForbidden): local.get_authorized(job['id'], 'viewer')
    with pytest.raises(MergeHeld, match='未完了'): local.finish_with_aliases(job['id'], 'manager')
    assert resolve_alias('project', 'zoho', 'old-zoho') is None
    local.reserve_step(job['id'], 0, 'manager')
    local.complete_step(job['id'], 0, 'manager', None)
    local.finish_with_aliases(job['id'], 'manager')
    assert resolve_alias('project', 'zoho', 'old-zoho') == 'target'
    assert resolve_alias('contact', 'zoho', 'old-zoho') is None
    assert local.get_authorized(job['id'], 'manager')['state'] == 'done'
    with connect() as conn:
        assert conn.execute('SELECT COUNT(*) AS n FROM "SyncOperationHistory"').fetchone()['n'] == 5


def test_history_failure_rolls_back_aliases_and_completion(local):
    job = prepare(local)
    local.complete_step(job['id'], 0, 'manager', None)
    with connect() as conn:
        conn.execute('ALTER TABLE "SyncOperationHistory" ADD CONSTRAINT synthetic_failure CHECK (false) NOT VALID')
    with pytest.raises(psycopg.errors.CheckViolation):
        local.finish_with_aliases(job['id'], 'manager')
    assert resolve_alias('project', 'zoho', 'old-zoho') is None
    assert local.get_authorized(job['id'], 'manager')['state'] == 'running'


def test_creation_dismissal_is_bound_to_source_and_external_snapshot(local):
    from src.record_merge.creation_candidates import candidate_context, hold_duplicate
    from src.hub_creation.domain import CreationHeld
    from src.record_merge.journal import history
    properties = {'グループ名': '合成チェーン'}
    external = {'id': 'z-test', 'Name': '合成チェーン', 'Modified_Time': 'v1'}
    def attempt(values, record):
        with candidate_context('chain', 'page-test', 'notion:page-test', values):
            hold_duplicate('zoho', 'chain', 'z-test', record, '候補あり')
    with pytest.raises(CreationHeld): attempt(properties, external)
    with connect() as conn:
        row = conn.execute('SELECT * FROM "RecordCreationCandidate"').fetchone()
        conn.execute('UPDATE "RecordCreationCandidate" SET state=\'dismissed\' WHERE id=%s', (row['id'],))
        history(conn, 'manager', row['id'], {}, {'state': 'creation_dismissed'})
    attempt(properties, external)
    with pytest.raises(CreationHeld): attempt({**properties, 'メモ': '変更'}, external)
    with connect() as conn:
        assert conn.execute('SELECT state FROM "RecordCreationCandidate"').fetchone()['state'] == 'pending'
        conn.execute('UPDATE "RecordCreationCandidate" SET state=\'dismissed\'')
    with pytest.raises(CreationHeld): attempt({**properties, 'メモ': '変更'}, {**external, 'Modified_Time': 'v2'})
    with connect() as conn:
        assert conn.execute('SELECT state FROM "RecordCreationCandidate"').fetchone()['state'] == 'pending'


def test_uncreated_reservation_reset_requires_confirmation_and_preserves_audit(local, monkeypatch):
    from contextlib import nullcontext
    from src.record_merge.creation_candidates import candidate_context, hold_duplicate, CreationCandidates, reset_import_preview, reset_import
    from src.record_merge.operations import MergeOperations
    from src.hub_creation.domain import CreationHeld
    monkeypatch.setattr('src.sync_engine.record_sync_lock.acquire_record_sync_locks', lambda *args: nullcontext())
    with candidate_context('chain', 'source', 'notion:source', {'グループ名': '合成'}):
        with pytest.raises(CreationHeld): hold_duplicate('zoho', 'chain', 'test-id', {'Name': '合成'}, '候補')
    with connect() as conn:
        row = conn.execute('UPDATE "RecordCreationCandidate" SET state=\'reserved\' RETURNING *').fetchone()
    operations = MergeOperations(None, {})
    def compare(self, actor, identifier):
        self.operations.authorize(actor)
        return {'snapshot': {'id': identifier, 'dbKey': 'chain', 'sourceId': 'source',
                             'tool': 'zoho', 'externalId': 'test-id', 'mappedId': None}}
    monkeypatch.setattr(CreationCandidates, 'compare', compare)
    identifier = row['id']
    preview = reset_import_preview(operations, 'manager', identifier)
    with pytest.raises(ReviewForbidden): reset_import_preview(operations, 'viewer', identifier)
    with pytest.raises(MergeHeld): reset_import(operations, 'manager', identifier, preview['hash'], '')
    with pytest.raises(MergeHeld): reset_import(operations, 'manager', identifier, 'stale', 'Notionとごみ箱で未作成を確認しました')
    assert reset_import(operations, 'manager', identifier, preview['hash'], 'Notionとごみ箱で未作成を確認しました')['state'] == 'pending'
    with pytest.raises(MergeHeld): reset_import_preview(operations, 'manager', identifier)
    with connect() as conn:
        assert conn.execute('SELECT "importedNotionId" FROM "RecordCreationCandidate"').fetchone()['importedNotionId'] is None
        assert conn.execute('SELECT "afterState" FROM "SyncOperationHistory"').fetchone()['afterState']['state'] == 'creation_reservation_reset'


@pytest.mark.parametrize("renamed", [False, True])
def test_old_relation_resume_after_delivery_failure_keeps_existing_relation(local, monkeypatch, renamed):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from src.record_merge.relation_decisions import RelationDecisions
    from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
    from src.db_schema.base import Tool
    from tests.record_merge.test_gateway import Client, raw
    from src.record_merge.notion_gateway import NotionMergeGateway
    from src.record_merge.operations import MergeOperations
    with connect() as conn:
        conn.execute('''CREATE TABLE "RelationReviewQueue" (id TEXT PRIMARY KEY,status TEXT,"targetDbKey" TEXT,
            "rawValue" TEXT,"candidateNotionPageIds" JSONB,"resolvedAt" TIMESTAMPTZ,"resolvedNotionPageId" TEXT)''')
        conn.execute('''INSERT INTO "RelationReviewQueue" VALUES ('review','pending','client_master','候補','["chosen"]',NULL,NULL)''')
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping('source', 'action', zoho_id='source-z'))
    store.upsert(IdMapping('chosen', 'client_master', zoho_id='chosen-z'))
    client = Client('action', {'source': raw('action', 'source', {'👨‍👩‍👧‍👦 取引先マスター': ['other']})})
    target = Client('client_master', {'chosen': raw('client_master', 'chosen', {'取引先名': '候補'})})
    dispatcher = SimpleNamespace(_targets={Tool.NOTION: SimpleNamespace(get_record=lambda *args, **kwargs: {})},
        propagate_linked_relation=Mock(side_effect=RuntimeError('合成配送失敗')))
    operations = MergeOperations(store, {'action': client, 'client_master': target}, dispatcher)
    service = RelationDecisions(operations)
    native_state = {'value': '候補', 'version': 'v1'}
    service.source_options = lambda row, **kwargs: [{'dbKey': 'action', 'notionId': 'source', 'property': '👨‍👩‍👧‍👦 取引先マスター',
        'native': native_state['value'], 'tool': 'kintone' if renamed else 'zoho', 'externalId': 'source-z', 'recordVersion': native_state['version']}]
    if renamed:
        target.pages['chosen'] = raw('client_master', 'chosen', {'取引先名': '正式名称'})
    preview = service.compare('manager', 'review', 'chosen')
    with pytest.raises(RuntimeError, match='配送'):
        service.approve('manager', 'review', preview['hash'], 'chosen', 'action', '👨‍👩‍👧‍👦 取引先マスター')
    with connect() as conn:
        assert conn.execute('SELECT state FROM "RelationReviewDecision"').fetchone()['state'] == 'approved'
        assert conn.execute('SELECT status FROM "RelationReviewQueue"').fetchone()['status'] == 'pending'
    assert client.pages['source']['properties']['👨‍👩‍👧‍👦 取引先マスター']['relation'] == [{'id': 'other'}, {'id': 'chosen'}]
    dispatcher.propagate_linked_relation.side_effect = None
    native_state['version'] = 'v2'
    if renamed: native_state['value'] = '正式名称'
    assert service.resume('manager', 'review')['state'] == 'resolved'
    with connect() as conn:
        assert conn.execute('SELECT status FROM "RelationReviewQueue"').fetchone()['status'] == 'resolved'
        assert conn.execute('SELECT "targetNotionId" FROM "RelationResolution"').fetchone()['targetNotionId'] == 'chosen'
        assert conn.execute('SELECT count(*) AS n FROM "SyncOperationHistory"').fetchone()['n'] == 2


def test_child_reservation_and_abandon_release_keep_partial_results(local):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from src.record_merge.operations import MergeOperations
    from src.record_merge.aliases import require_record_available
    from src.sync_engine.record_sync_lock import RecordSyncBusy
    snapshot = {'dbKey': 'client_master', 'sourceId': 's', 'targetId': 't',
                'children': [{'dbKey': 'action', 'id': 'child'}], 'aliases': {'notion': 's'}}
    steps = [{'kind': 'archive', 'dbKey': 'client_master', 'id': 's', 'before': False, 'desired': True}]
    job = local.create(snapshot, steps, 'manager')
    local.decide(job['id'], 'manager', job['planHash'], 'approve')
    with pytest.raises(RecordSyncBusy): require_record_available('action', 'child')
    operations = MergeOperations(None, {})
    operations.locks = lambda job: nullcontext()
    operations.gateway = SimpleNamespace(page=lambda *args: {'archived': False}, read=lambda step: False)
    preview = operations.preview_abandon('manager', job['id'])
    with pytest.raises(MergeHeld): operations.abandon('manager', job['id'], 'changed')
    assert operations.abandon('manager', job['id'], preview['hash'])['state'] == 'abandoned'
    require_record_available('action', 'child')
    assert resolve_alias('client_master', 'notion', 's') is None
    with connect() as conn:
        row = conn.execute('SELECT "afterState" FROM "SyncOperationHistory" ORDER BY "createdAt" DESC LIMIT 1').fetchone()
        assert row['afterState']['partialChangesRetained'] is True


def test_alias_chain_and_cycle_remain_guarded_when_operations_disabled(local, monkeypatch):
    job = prepare(local)
    with connect() as conn:
        for old, target in [('a', 'b'), ('b', 'c')]:
            conn.execute('INSERT INTO "RecordMergeAlias" ("dbKey",tool,"oldId","targetId","operationId") VALUES (%s,%s,%s,%s,%s)',
                         ('project', 'notion', old, target, job['id']))
    monkeypatch.setenv('RECORD_MERGE_ENABLED', 'false')
    assert resolve_alias('project', 'notion', 'a') == 'c'
    with connect() as conn:
        conn.execute('INSERT INTO "RecordMergeAlias" ("dbKey",tool,"oldId","targetId","operationId") VALUES (%s,%s,%s,%s,%s)',
                     ('project', 'notion', 'c', 'a', job['id']))
    with pytest.raises(MergeHeld, match='循環'): resolve_alias('project', 'notion', 'a')


def test_creation_import_unknown_result_never_reposts_after_input_change(local):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from src.record_merge.creation_candidates import candidate_context, hold_duplicate, CreationCandidates
    from src.hub_creation.domain import CreationHeld
    from src.record_merge.operations import MergeOperations
    from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
    from src.db_schema.base import Tool
    from tests.record_merge.test_gateway import Client, raw
    store = SQLiteIdMappingStore(); store.upsert(IdMapping('source', 'chain'))
    external = {'id': 'candidate', 'Name': '合成チェーン', 'field3': '0'}
    client = Client('chain', {'source': raw('chain', 'source', {'グループ名': '合成チェーン'})})
    client.create_page_once = Mock(side_effect=TimeoutError('合成応答不明'))
    dispatcher = SimpleNamespace(_targets={Tool.ZOHO: SimpleNamespace(get_record=lambda *args, **kwargs: external)})
    candidates = CreationCandidates(MergeOperations(store, {'chain': client}, dispatcher))
    with candidate_context('chain', 'source', 'notion:source', {'グループ名': '合成チェーン'}):
        with pytest.raises(CreationHeld): hold_duplicate('zoho', 'chain', 'candidate', external, '同名候補')
    with connect() as conn: identifier = conn.execute('SELECT id FROM "RecordCreationCandidate"').fetchone()['id']
    preview = candidates.compare('manager', identifier)
    with pytest.raises(TimeoutError): candidates.decide('manager', identifier, preview['hash'], 'import_creation')
    with candidate_context('chain', 'source', 'notion:source', {'グループ名': '変更後'}):
        with pytest.raises(CreationHeld, match='開始済み'): hold_duplicate('zoho', 'chain', 'candidate', external, '同名候補')
    preview = candidates.compare('manager', identifier)
    with pytest.raises(MergeHeld, match='結果が不明'): candidates.decide('manager', identifier, preview['hash'], 'import_creation')
    assert client.create_page_once.call_count == 1
    with connect() as conn: assert conn.execute('SELECT state FROM "RecordCreationCandidate"').fetchone()['state'] == 'reserved'
