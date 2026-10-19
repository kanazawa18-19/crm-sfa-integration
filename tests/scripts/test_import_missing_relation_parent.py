"""取込漏れ運用の中断点と再POST禁止を検証する。実サービスには接続しない。"""
from contextlib import nullcontext
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.import_missing_relation_parent import ParentImport
from src.record_merge.domain import MergeHeld, digest
from src.sync_engine.id_mapping import IdMapping


@pytest.fixture
def importer(monkeypatch):
    monkeypatch.setattr('scripts.import_missing_relation_parent.acquire_record_sync_lock', lambda *a: nullcontext())
    monkeypatch.setattr('src.record_merge.aliases.require_record_available', lambda *a: None)
    monkeypatch.setattr('src.record_merge.aliases.resolve_alias', lambda *a: None)
    client = Mock()
    store = Mock()
    store.get.return_value = None
    journal = Mock()
    journal.row = None
    journal.get.side_effect = lambda *a: deepcopy(journal.row)
    journal.reserve.side_effect = lambda key, target, db, fingerprint: setattr(journal, 'row', {
        'dbKey': db, 'identityHash': fingerprint, 'state': 'reserved', 'externalId': None}) or True
    journal.finish.side_effect = lambda key, target, identifier: journal.row.update(state='created', externalId=identifier)
    op = SimpleNamespace(store=store, gateway=SimpleNamespace(clients={'client_master': client}),
                         dispatcher=Mock(), authorize=Mock())
    imp = ParentImport(op, journal, 'manager')
    snapshot = {'version': 1, 'reviewId': 'review', 'externalId': '123',
                'source': {'notionId': 'action'}, 'properties': {'取引先名': '試験\\u3000旅館'}}
    imp.snapshot = Mock(return_value=snapshot)
    imp.verify_imported = Mock(return_value=None)
    imp.verify_external_binding = Mock()
    imp.mappings = Mock(return_value=[IdMapping('parent', 'client_master', zoho_id='123')])
    client.create_page_once.return_value = 'parent'
    return imp, {'snapshot': snapshot, 'hash': digest(snapshot)}


def test_reservation_precedes_post_and_finish_precedes_mapping(importer):
    imp, plan = importer
    def create(*a, **kw):
        assert imp.journal.row['state'] == 'reserved'
        assert kw['children'][0]['paragraph']['rich_text'][0]['text']['content'] == imp.marker(plan)
        return 'parent'
    imp.client.create_page_once.side_effect = create
    def save(*a, **kw):
        assert imp.journal.row['state'] == 'created'
        assert imp.verify_imported.call_count == 1
    imp.store.upsert.side_effect = save
    assert imp.apply(plan)['notionId'] == 'parent'
    imp.client.create_page_once.assert_called_once()
    assert imp.verify_imported.call_count == 2


def test_timeout_stays_reserved_and_never_posts_again(importer):
    imp, plan = importer
    imp.client.create_page_once.side_effect = TimeoutError
    with pytest.raises(TimeoutError): imp.apply(plan)
    with pytest.raises(MergeHeld, match='結果が不明'): imp.apply(plan)
    assert imp.journal.row['state'] == 'reserved'
    imp.client.create_page_once.assert_called_once()
    imp.store.upsert.assert_not_called()


def test_finish_failure_never_posts_again(importer):
    imp, plan = importer
    imp.journal.finish.side_effect = RuntimeError
    with pytest.raises(RuntimeError): imp.apply(plan)
    with pytest.raises(MergeHeld, match='結果が不明'): imp.apply(plan)
    imp.client.create_page_once.assert_called_once()


def test_mapping_failure_resumes_known_id_only(importer):
    imp, plan = importer
    imp.store.upsert.side_effect = [RuntimeError(), None]
    with pytest.raises(RuntimeError): imp.apply(plan)
    assert imp.apply(plan)['state'] == 'imported'
    imp.client.create_page_once.assert_called_once()
    assert imp.verify_imported.call_count == 3


def test_live_source_changed_does_not_reserve(importer):
    imp, plan = importer
    imp.snapshot.return_value = {**plan['snapshot'], 'new': '変更'}
    with pytest.raises(MergeHeld, match='計画後'): imp.apply(plan)
    imp.journal.reserve.assert_not_called()
    imp.client.create_page_once.assert_not_called()


def test_resumed_target_bad_marker_or_value_does_not_map(importer):
    imp, plan = importer
    imp.journal.row = {'dbKey': 'client_master', 'identityHash': plan['hash'], 'state': 'created', 'externalId': 'parent'}
    imp.verify_imported.side_effect = MergeHeld('確認番号が不一致')
    with pytest.raises(MergeHeld): imp.apply(plan)
    imp.client.create_page_once.assert_not_called()
    imp.store.upsert.assert_not_called()


def test_existing_mapping_is_not_overwritten(importer):
    imp, plan = importer
    imp.verify_imported.return_value = IdMapping('parent', 'client_master', zoho_id='123', spreadsheet_row=99)
    imp.apply(plan)
    imp.store.upsert.assert_not_called()


def test_foreign_plan_and_tampered_plan_blocked(importer):
    imp, plan = importer
    imp.journal.row = {'dbKey': 'client_master', 'identityHash': 'other', 'state': 'created', 'externalId': 'parent'}
    with pytest.raises(MergeHeld, match='別の計画'): imp.apply(plan)
    with pytest.raises(MergeHeld, match='計画ファイル'): imp.apply({**plan, 'hash': 'bad'})
    imp.client.create_page_once.assert_not_called()


def test_actual_value_and_marker_verification_preserves_literal_text(importer):
    imp, plan = importer
    page = {'properties': {'取引先名': {'type': 'title', 'title': [{'plain_text': '試験\\u3000旅館'}]}}}
    imp.op.gateway.page = Mock(return_value=page)
    imp.op.gateway.blocks = Mock(return_value=[{'paragraph': {'rich_text': [{'plain_text': imp.marker(plan)}]}}])
    assert ParentImport.verify_imported(imp, plan, 'parent') is None
    page['properties']['取引先名']['title'][0]['plain_text'] = '試験 旅館'
    with pytest.raises(MergeHeld, match='項目'): ParentImport.verify_imported(imp, plan, 'parent')
    page['properties']['取引先名']['title'][0]['plain_text'] = '試験\\u3000旅館'
    imp.op.gateway.blocks.return_value = []
    with pytest.raises(MergeHeld, match='確認番号'): ParentImport.verify_imported(imp, plan, 'parent')


def test_title_lookup_is_all_pages_and_does_not_modify_title(importer):
    imp, _ = importer
    imp.client.query_all_pages.return_value = [{'id': 'second-page-match'}]
    assert imp.title_candidates('試験\\u3000旅館') == {'second-page-match'}
    filters = imp.client.query_all_pages.call_args.kwargs['filter']['or']
    assert {'property': '取引先名', 'title': {'contains': '試験\\u3000旅館'}} in filters
    assert {'and': [{'property': '取引先名', 'title': {'contains': '旅館'}},
                    {'property': '取引先名', 'title': {'contains': '試験'}}]} in filters


@pytest.mark.parametrize('problem', ['source_id', 'mapping_duplicate', 'title_candidate', 'alias', 'archived', 'relation_present'])
def test_snapshot_rejects_unsafe_intake_before_post(importer, monkeypatch, problem):
    imp, plan = importer
    imp.dec = Mock()
    imp.dec.queue_row.return_value = {'sourceTool': 'zoho', 'targetDbKey': 'client_master'}
    source = {'field': 'field6', 'native': {'id': '123'}, 'notionId': 'action', 'property': '親'}
    imp.dec.source_options.return_value = [source]
    imp.op.gateway.page = Mock(return_value={})
    imp.op.gateway.values = Mock(return_value={'親': []})
    imp.op.gateway.blocks = Mock(return_value=[])
    imp.mappings.return_value = []
    imp.title_candidates = Mock(return_value=set())
    imp.op.dispatcher._targets = {__import__('src.db_schema.base', fromlist=['Tool']).Tool.ZOHO: Mock()}
    target = next(iter(imp.op.dispatcher._targets.values()))
    target.get_record.return_value = {'id': '123', 'Account_Name': '試験名'}
    monkeypatch.setattr('scripts.import_missing_relation_parent.build_notion_properties_for_new_record',
                        lambda **kw: {'取引先名': '試験名'})
    if problem == 'source_id': source['native']['id'] = '456'
    if problem == 'mapping_duplicate': imp.mappings.return_value = [IdMapping('one', 'client_master'), IdMapping('two', 'client_master')]
    if problem == 'title_candidate': imp.title_candidates.return_value = {'other'}
    if problem == 'alias': monkeypatch.setattr('src.record_merge.aliases.resolve_alias', lambda *a: 'canonical')
    if problem == 'archived': imp.op.gateway.page.return_value = {'archived': True}
    if problem == 'relation_present': imp.op.gateway.values.return_value = {'親': ['existing']}
    with pytest.raises(MergeHeld): ParentImport.snapshot(imp, 'review', '123')
    imp.client.create_page_once.assert_not_called()
    imp.journal.reserve.assert_not_called()


def test_created_resume_after_real_snapshot_source_and_parent_change(importer, monkeypatch):
    """最初は実snapshot経路。POST後の活動日時・元actionの編集では既知ID回収を妨げない。"""
    from src.db_schema.base import Tool
    imp, _ = importer
    imp.dec = Mock()
    imp.dec.queue_row.return_value = {'sourceTool': 'zoho', 'targetDbKey': 'client_master'}
    imp.dec.source_options.return_value = [{'field': 'field6', 'native': {'id': '123'},
                                           'notionId': 'action', 'property': '親'}]
    imp.op.gateway.page = Mock(return_value={})
    imp.op.gateway.values = Mock(return_value={'親': [], '本文': '元の本文'})
    imp.op.gateway.blocks = Mock(return_value=[])
    imp.mappings.return_value = []
    imp.title_candidates = Mock(return_value=set())
    native = {'id': '123', 'Account_Name': '試験名', 'Modified_Time': '2026-10-19T01:00:00Z'}
    target = Mock(); target.get_record.return_value = native
    imp.op.dispatcher._targets = {Tool.ZOHO: target}
    monkeypatch.setattr('scripts.import_missing_relation_parent.build_notion_properties_for_new_record',
                        lambda **kw: {'取引先名': kw['raw_record']['Account_Name']})
    imp.snapshot = lambda *a, **kw: ParentImport.snapshot(imp, *a, **kw)
    imp.verify_external_binding = lambda *a: ParentImport.verify_external_binding(imp, *a)
    plan = deepcopy(imp.preview('review', '123'))
    imp.store.upsert.side_effect = RuntimeError('対応保存失敗')
    with pytest.raises(RuntimeError): imp.apply(plan)
    native['Modified_Time'] = '2026-10-19T02:00:00Z'
    imp.op.gateway.values.return_value = {'親': ['parent'], '本文': '後の本文'}
    imp.dec.queue_row.side_effect = MergeHeld('関連承認済み')
    imp.store.upsert.side_effect = lambda *a, **kw: setattr(imp.mappings, 'return_value', [IdMapping('parent', 'client_master', zoho_id='123')])
    assert imp.apply(plan)['notionId'] == 'parent'
    imp.client.create_page_once.assert_called_once()


def test_mapped_page_normal_changes_keep_marker_and_identity_checks(importer):
    imp, plan = importer
    imp.store.get.return_value = IdMapping('parent', 'client_master', zoho_id='123')
    imp.op.gateway.page = Mock(return_value={'properties': {'取引先名': {'type': 'title', 'title': [{'plain_text': '通常同期後の名称'}]}}})
    imp.op.gateway.blocks = Mock(return_value=[{'paragraph': {'rich_text': [{'plain_text': imp.marker(plan)}]}}])
    assert ParentImport.verify_imported(imp, plan, 'parent').zoho_id == '123'
    imp.store.get.return_value = IdMapping('parent', 'client_master', zoho_id='456')
    with pytest.raises(MergeHeld, match='外部ID'): ParentImport.verify_imported(imp, plan, 'parent')


@pytest.mark.parametrize('problem', ['missing', 'alias', 'other_mapping', 'duplicate_mapping'])
def test_created_resume_rejects_wrong_external_identity(importer, monkeypatch, problem):
    from src.db_schema.base import Tool
    imp, plan = importer
    target = Mock(); target.get_record.return_value = {'id': '123'}
    imp.op.dispatcher._targets = {Tool.ZOHO: target}
    if problem == 'missing': target.get_record.return_value = None
    if problem == 'alias': monkeypatch.setattr('src.record_merge.aliases.resolve_alias', lambda *a: 'other')
    if problem == 'other_mapping': imp.mappings.return_value = [IdMapping('other', 'client_master', zoho_id='123')]
    if problem == 'duplicate_mapping': imp.mappings.return_value *= 2
    with pytest.raises(MergeHeld): ParentImport.verify_external_binding(imp, plan, 'parent')
