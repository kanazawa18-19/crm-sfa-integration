"""実担当者の根拠、アカウント不在、コメント往復を外部I/Oなしで確認する。"""
import json
from types import SimpleNamespace
import pytest
from src.hub_creation.action_kintone import (BEGIN, END, action_actors, action_service,
    comment_with_actor, preserve_comment, split_comment)
from src.hub_creation.domain import CreationHeld
from src.record_merge.creation_candidates import candidate_context, creation_context

PERSON = '11111111-1111-1111-1111-111111111111'
PAGE = '22222222-2222-2222-2222-222222222222'
OTHER = '33333333-3333-3333-3333-333333333333'


@pytest.fixture
def directory(monkeypatch):
    data = {'verified': True, 'fallback_notion_id': OTHER, 'users': [
        {'notion_id': PERSON, 'display_name': '検証担当者', 'enabled': True},
        {'notion_id': OTHER, 'display_name': '検証管理者', 'kintone_code': 'manager', 'enabled': True}]}
    monkeypatch.setenv('CRM_USER_MAPPING_JSON', json.dumps(data))
    return data


def configure_override(monkeypatch):
    monkeypatch.setenv('CRM_ACTION_ACTORS_JSON', json.dumps({'verified': True, 'records': {
        PAGE: {'notion_user_id': PERSON, 'expected_created_by': PERSON}}}))


def test_unmapped_explicit_actor_is_not_replaced_by_fallback(directory):
    assert action_actors({'担当営業': [PERSON]}, None) == ([], ['検証担当者'])
    assert action_actors({'担当営業': [OTHER]}, None) == ([{'code': 'manager'}], [])


def test_approved_page_override_requires_matching_creator(directory, monkeypatch):
    configure_override(monkeypatch)
    context = {'sourceId': PAGE, 'action_created_by': PERSON}
    assert action_actors({'担当営業': []}, context) == ([], ['検証担当者'])
    for bad in [None, {**context, 'sourceId': OTHER}, {**context, 'action_created_by': OTHER}]:
        with pytest.raises(CreationHeld, match='実担当者が未確定'):
            action_actors({'担当営業': []}, bad)


def test_creator_without_explicit_approval_is_not_the_actor(directory, monkeypatch):
    monkeypatch.delenv('CRM_ACTION_ACTORS_JSON', raising=False)
    with pytest.raises(CreationHeld, match='実担当者が未確定'):
        action_actors({'担当営業': []}, {'sourceId': PAGE, 'action_created_by': PERSON})


def test_unfinished_rollup_does_not_use_approved_override(directory, monkeypatch):
    configure_override(monkeypatch)
    with pytest.raises(CreationHeld, match='集計が未確定'):
        action_actors({}, {'sourceId': PAGE, 'action_created_by': PERSON})


def test_mixed_mapped_and_unmapped_people_are_all_named_without_partial_assignment(directory):
    assert action_actors({'担当営業': [PERSON, OTHER]}, None) == ([], ['検証担当者', '検証管理者'])


def test_empty_confirmed_services_only_use_existing_unknown_option():
    fields = {'service': {'options': {'未確定': {}, '検証サービス': {}}}}
    assert action_service({'提案サービス': []}, fields) == ['未確定']
    assert action_service({'提案サービス': ['検証サービス']}, fields) == ['検証サービス']
    for props, metadata in [({}, fields), ({'提案サービス': None}, fields), ({'提案サービス': []}, {})]:
        with pytest.raises(CreationHeld):
            action_service(props, metadata)


def test_comment_roundtrip_keeps_exact_user_body_and_one_actor_block():
    from src.sync_engine.webhook_handlers.kintone_field_transforms import _action_comment_body
    body = '先方の回答\n\n次回の予定\n'
    comment = comment_with_actor(body, ['検証担当者'])
    assert _action_comment_body(comment) == body
    new_comment = preserve_comment(comment, '本文を更新\n')
    assert _action_comment_body(new_comment) == '本文を更新\n'
    assert new_comment.count(BEGIN) == 1
    assert preserve_comment(new_comment, new_comment) == new_comment
    assert _action_comment_body(preserve_comment(new_comment, None)) is None
    assert '検証担当者' in preserve_comment(new_comment, None)


@pytest.mark.parametrize('bad', [BEGIN, END, END + BEGIN, BEGIN + '\n任意本文\n' + END,
    BEGIN + '\n実担当者: \n' + END])
def test_malformed_actor_blocks_are_held_not_deleted(bad):
    from src.sync_engine.webhook_handlers.kintone_field_transforms import _action_comment_body, SKIP_FIELD
    with pytest.raises(ValueError):
        preserve_comment(bad, '更新')
    assert _action_comment_body(bad) is SKIP_FIELD
    valid = comment_with_actor('本文', ['検証担当者'])
    with pytest.raises(ValueError):
        preserve_comment(valid + '\n' + valid, '更新')


def test_normal_sync_preserves_actor_and_uses_current_revision():
    from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
    saved = comment_with_actor('旧本文', ['検証担当者'])
    writes = []
    client = SimpleNamespace(get_record=lambda *a: {'comment': saved, '$revision': '7'},
        update_record=lambda *a, **kw: writes.append((a, kw)))
    target = KintoneSyncTarget(client, '1')
    assert target.upsert_record('42', {'履歴メモ': '新本文'}, db_key='action', expected_version='7') == '42'
    args, kwargs = writes[0]
    assert split_comment(args[2]['comment'])[0] == '新本文'
    assert '検証担当者' in args[2]['comment']
    assert kwargs['expected_version'] == '7'
    assert 'cnctorMember' not in args[2]


def test_normal_sync_does_not_overwrite_concurrent_comment_edit():
    from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
    from src.sync_engine.clients._http import ConcurrentModificationError
    client = SimpleNamespace(get_record=lambda *a: {'comment': '別編集', '$revision': '8'},
        update_record=lambda *a, **kw: pytest.fail('競合時は書き込みません'))
    with pytest.raises(ConcurrentModificationError):
        KintoneSyncTarget(client, '1').upsert_record('42', {'履歴メモ': '新本文'}, db_key='action', expected_version='7')


def test_action_creation_keeps_empty_user_field_and_unknown_service(directory, monkeypatch):
    from src.hub_creation import adapters
    configure_override(monkeypatch)
    fields = {'cnctorMember': {'type': 'USER_SELECT', 'required': False},
        'service': {'type': 'MULTI_SELECT', 'required': True, 'options': {'未確定': {}}},
        'comment': {'type': 'MULTI_LINE_TEXT', 'required': True},
        'client_name': {'type': 'SINGLE_LINE_TEXT', 'required': True},
        'actionContent': {'type': 'DROP_DOWN', 'options': {'テレアポ': {}}}}
    candidates = []
    monkeypatch.setattr(adapters, 'request_with_retry', lambda method, url, **kw:
        SimpleNamespace(ok=True, status_code=200, json=lambda: {'properties': fields} if 'fields.json' in url else {'records': candidates}))
    client = SimpleNamespace(_domain='example.invalid', _headers=lambda **kw: {}, _timeout=1,
        _max_retries=0, _backoff_base=0, get_record=lambda *a: {'顧客名': '検証施設'})
    targets = {key: SimpleNamespace(_client=client, _app='1') for key in ['action', 'client_master']}
    store = SimpleNamespace(get=lambda _: SimpleNamespace(db_key='client_master', kintone_id='42'))
    props = {'担当営業': [], '提案サービス': [], '履歴メモ': '接触の記録',
        'アクション種別': 'テレアポ', '👨‍👩‍👧‍👦 取引先マスター': [OTHER]}
    with candidate_context('action', PAGE, 'notion:' + PAGE, props):
        creation_context()['action_created_by'] = PERSON
        result = adapters.KintoneCreationAdapter(targets, store).plan('action', props)
    assert result['cnctorMember'] == []
    assert result['service'] == ['未確定']
    assert split_comment(result['comment']) == ('接触の記録', BEGIN + '\n実担当者: 検証担当者\n' + END)
    for candidate_comment in ['接触の記録', result['comment'].replace('\n', '\r\n')]:
        candidates[:] = [{'$id': {'value': '99'}, **{key: {'value': value} for key, value in result.items()},
                         'comment': {'value': candidate_comment}}]
        with candidate_context('action', PAGE, 'notion:' + PAGE, props):
            creation_context()['action_created_by'] = PERSON
            with pytest.raises(CreationHeld, match='一致'):
                adapters.KintoneCreationAdapter(targets, store).plan('action', props)
    candidates.clear()
    fields['cnctorMember']['required'] = True
    with candidate_context('action', PAGE, 'notion:' + PAGE, props):
        creation_context()['action_created_by'] = PERSON
        with pytest.raises(CreationHeld, match='必須'):
            adapters.KintoneCreationAdapter(targets, store).plan('action', props)


def test_existing_zoho_is_not_planned_or_resent_when_kintone_resumes(directory, monkeypatch):
    from tests.hub_creation.test_action_identity import action_setup, event
    from src.sync_engine.id_mapping import IdMapping
    service, journal, notion, adapter, pages = action_setup()
    configure_override(monkeypatch)
    original = pages.pop('page')
    original['id'] = PAGE
    original['created_by'] = {'id': PERSON}
    for name in ('担当営業', '提案サービス'):
        original['properties'][name] = {'type': 'rollup', 'rollup': {'type': 'array', 'array': []}}
    pages[PAGE] = original
    service.store.upsert(IdMapping(notion_key=PAGE, db_key='action', zoho_id='existing-zoho'))
    journal.hold('notion:' + PAGE, 'source', 'action', 'kintone待ち')
    zoho = SimpleNamespace(target='zoho', plan=lambda *a: pytest.fail('既存Zohoは計画しません'),
        create=lambda *a: pytest.fail('既存Zohoは再送しません'))
    seen = []
    adapter.target = 'kintone'
    adapter.plan = lambda db, props: seen.append(action_actors(props, creation_context())) or props
    service.adapters = [zoho, adapter]
    assert service.handle(event(PAGE)) == 'hub_creation_complete'
    assert seen == [([], ['検証担当者'])]
    assert service.store.get(PAGE).zoho_id == 'existing-zoho'
    assert adapter.creates == 1


def test_sync_with_missing_current_comment_cannot_drop_actor_block():
    from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
    from src.sync_engine.clients._http import ApiError
    client = SimpleNamespace(get_record=lambda *a: {'$revision': '7'},
        update_record=lambda *a, **kw: pytest.fail('不完全応答から書き込みません'))
    with pytest.raises(ApiError):
        KintoneSyncTarget(client, '1').upsert_record('42', {'履歴メモ': '新本文'}, db_key='action')


@pytest.mark.parametrize('value', [BEGIN, BEGIN + '\n任意本文\n' + END, 123])
def test_actor_comment_shape_error_is_a_reasoned_creation_hold(value):
    with pytest.raises(CreationHeld, match='形式|破損'):
        comment_with_actor(value, ['検証担当者'])


@pytest.mark.parametrize('actor', [PERSON, OTHER])
@pytest.mark.parametrize('comment', [BEGIN, BEGIN + '\n実担当者: 検証担当者\n' + END])
def test_creation_rejects_managed_block_even_for_fully_mapped_actors(directory, monkeypatch, actor, comment):
    from src.hub_creation import adapters
    fields = {'comment': {'type': 'MULTI_LINE_TEXT'}, 'cnctorMember': {'type': 'USER_SELECT'}}
    monkeypatch.setattr(adapters, 'request_with_retry', lambda *a, **kw:
        SimpleNamespace(ok=True, status_code=200, json=lambda: {'properties': fields}))
    client = SimpleNamespace(_domain='example.invalid', _headers=lambda **kw: {},
        _timeout=1, _max_retries=0, _backoff_base=0)
    adapter = adapters.KintoneCreationAdapter({'action': SimpleNamespace(_client=client, _app='1')})
    with pytest.raises(CreationHeld, match='補足枠'):
        adapter.plan('action', {'担当営業': [actor], '提案サービス': [], '履歴メモ': comment,
                              '👨‍👩‍👧‍👦 取引先マスター': [OTHER]})


@pytest.mark.parametrize('suffix', ['\n追記した本文', '追記した本文', '\n'])
def test_nonterminal_actor_block_is_held_without_reordering_body(suffix):
    from src.sync_engine.webhook_handlers.kintone_field_transforms import _action_comment_body, SKIP_FIELD
    comment = comment_with_actor('先頭の本文', ['検証担当者']) + suffix
    with pytest.raises(ValueError, match='末尾以外'):
        preserve_comment(comment, '新本文')
    assert _action_comment_body(comment) is SKIP_FIELD


@pytest.mark.parametrize('newline', ['\n', '\r\n'])
@pytest.mark.parametrize('separator', ['\n\n', '\r\n\r\n'])
def test_actor_block_newline_formats_preserve_body_separator_and_block(newline, separator):
    from src.sync_engine.webhook_handlers.kintone_field_transforms import _action_comment_body
    body = '先頭\r\n本文\n末尾\r\n'
    block = newline.join([BEGIN, '実担当者: 検証担当者', END])
    comment = body + separator + block
    assert split_comment(comment) == (body, block)
    assert _action_comment_body(comment) == body
    assert preserve_comment(comment, body) == comment
    updated = '更新\n本文\r\n'
    assert preserve_comment(comment, updated) == updated + separator + block
    assert preserve_comment(comment, comment) == comment


def test_mixed_newlines_inside_actor_block_remain_malformed():
    malformed = BEGIN + '\r\n実担当者: 検証担当者\n' + END
    with pytest.raises(ValueError, match='破損'):
        split_comment(malformed)


def test_normal_sync_full_properties_cannot_reintroduce_owner_or_service_assignment():
    from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
    writes, reads = [], []
    comment = comment_with_actor('旧本文', ['検証担当者'])
    def read(*args):
        reads.append(args)
        return {'comment': comment, '$revision': '7'}
    client = SimpleNamespace(get_record=read, update_record=lambda *a, **kw: writes.append(a[2]))
    KintoneSyncTarget(client, '1').upsert_record('42', {'履歴メモ': '新本文', '担当営業': [PERSON],
        '提案サービス': [], 'アクション種別': 'テレアポ'}, db_key='action', expected_version='7')
    assert len(reads) == 1
    assert 'cnctorMember' not in writes[0] and 'service' not in writes[0]
    assert writes[0]['comment'].count(BEGIN) == 1


def test_csv_action_import_keeps_only_user_comment_body():
    from src.migration.action_mapping import transform_kintone_action
    comment = comment_with_actor('利用者本文', ['検証担当者'])
    result = transform_kintone_action({'アクション内容': 'テレアポ', 'コメント': comment})
    assert result['履歴メモ'] == '利用者本文'
    with pytest.raises(ValueError):
        transform_kintone_action({'アクション内容': 'テレアポ', 'コメント': BEGIN})


def test_real_kintone_client_contract_flattens_revision_and_preserves_comment(requests_mock):
    from src.sync_engine.clients.kintone_client import HttpKintoneClient
    client = HttpKintoneClient('example.invalid', api_token='test-only')
    comment = comment_with_actor('本文', ['検証担当者'])
    requests_mock.get('https://example.invalid/k/v1/record.json', json={'record': {
        '$revision': {'type': '__REVISION__', 'value': '7'},
        'comment': {'type': 'MULTI_LINE_TEXT', 'value': comment}}})
    result = client.get_record('1', '42')
    assert result['$revision'] == '7' and result['comment'] == comment
