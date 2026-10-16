"""文字数上限・原文保持・戻り通知を、外部接続なしで検証する。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.db_schema.base import Tool
from src.sync_engine.clients._http import ApiError
from src.sync_engine.clients.notion_client import NOTION_LAST_EDITED_TIME_KEY
from src.sync_engine.decided_choices import merge_choice_memo, split_controller_memo
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_targets.zoho_sync import ZohoSyncTarget
from src.sync_engine.zoho_text_limits import limit_payload, shorten
from tests.sync_engine.test_dispatcher import FakeSyncTarget

FIELDS = [{'api_name': 'field12', 'data_type': 'text', 'length': 255}]


def test_limit_boundary_unicode_and_identifiers():
    original = '日本語😀' * 75
    payload = {'field12': original, 'Account_Name': original, 'Phone': original,
               'Email': original, 'Website': original, 'id': original}
    result = limit_payload('client_master', payload, FIELDS + [
        {'api_name': code, 'data_type': 'text', 'length': 3} for code in payload if code != 'field12'])
    assert result['field12'] == original[:255]
    assert all(result[k] == v for k, v in payload.items() if k != 'field12')
    assert payload['field12'] == original
    assert '300文字から255文字' in next(iter(result.text_limit_notes.values()))
    for size in (0, 254, 255):
        result = limit_payload('client_master', {'field12': 'あ' * size}, FIELDS)
        assert result['field12'] == 'あ' * size
        assert not any(result.text_limit_notes.values())


@pytest.mark.parametrize('field', [
    {}, {'length': '255'}, {'length': True}, {'length': 0},
    {'length': 255, 'data_type': 'email'}, {'length': 255, 'data_type': 'picklist'},
])
def test_unknown_limit_or_non_text_is_never_guessed(field):
    value = 'あ' * 300
    result = limit_payload('client_master', {'field12': value}, [{'api_name': 'field12', **field}])
    assert result['field12'] == value


def test_managed_block_survives_and_oversized_block_stops():
    memo = merge_choice_memo('あ' * 300, 'サイトコントローラー', ['リンカーン', 'ねっぱん'], limit=None)
    body, block = split_controller_memo(shorten(memo, 255, managed=True))
    assert len(body) + len(block) + 2 == 255
    assert 'リンカーン、ねっぱん' in block
    assert shorten(memo, len(block), managed=True) == block
    with pytest.raises(ValueError, match='管理用メモ'):
        shorten(memo, len(block) - 1, managed=True)


class Client:
    def __init__(self):
        self.fields = FIELDS
        self.record = {'Modified_Time': '2026-10-01T00:00:00Z'}
        self.writes = []
        self.reads = 0
        self.fail = False

    def get_fields(self, module):
        self.reads += 1
        return self.fields

    def get_record(self, module, record_id):
        return self.record.copy()

    def insert_record(self, module, payload):
        self.update_record(module, 'z1', payload)
        return 'z1'

    def update_record(self, module, record_id, payload, **kwargs):
        if self.fail:
            raise ApiError(422, 'テスト用エラー')
        self.writes.append(dict(payload))
        self.record.update(payload)


def dispatch(source, properties, *, client=None, original=''):
    client = client or Client()
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key='p1', db_key='client_master', zoho_id='z1',
                           last_synced_at=now - timedelta(days=1)), expected_last_synced_at=None)
    notion = FakeSyncTarget(Tool.NOTION, {'p1': {'備考': original,
                                               NOTION_LAST_EDITED_TIME_KEY: now - timedelta(hours=1)}})
    notes = []
    target = ZohoSyncTarget(client, 'Accounts', enabled=True)
    result = Dispatcher(store, {Tool.NOTION: notion, Tool.ZOHO: target},
                        note_writer=lambda event, mapping: notes.append(dict(event.sync_notes))).dispatch(
        SyncEvent(source, 'client_master', 'p1' if source is Tool.NOTION else 'z1', now, properties))
    return client, notion, notes, result


def test_update_records_omission_and_does_not_touch_master():
    value = 'あ' * 299
    client, notion, notes, result = dispatch(Tool.NOTION, {'備考': value}, original=value)
    assert client.writes == [{'field12': value[:255]}]
    assert not notion.upsert_calls
    assert any('299文字から255文字' in v for v in notes[-1].values())
    assert Tool.ZOHO in result.properties[0].written_tools


def test_failed_write_does_not_claim_omission_succeeded():
    client = Client()
    client.fail = True
    from src.sync_engine.zoho_text_limits import collect_text_limit_notes, collected_notes
    with collect_text_limit_notes():
        with pytest.raises(ApiError):
            ZohoSyncTarget(client, 'Accounts').upsert_record('z1', {'備考': 'あ' * 299}, db_key='client_master')
        assert not collected_notes()


def test_metadata_failure_stops_before_write():
    client = Client()
    def fail(module):
        raise ApiError(503, '設定取得失敗')
    client.get_fields = fail
    with pytest.raises(ApiError):
        dispatch(Tool.NOTION, {'備考': 'あ' * 299}, client=client)
    assert client.writes == []


def test_unrelated_update_does_not_read_metadata():
    client = Client()
    ZohoSyncTarget(client, 'Accounts').upsert_record('z1', {'取引先名': 'テスト法人'}, db_key='client_master')
    assert client.reads == 0


def test_shortened_echo_does_not_overwrite_master_but_real_edit_does():
    original = 'あ' * 299
    _, notion, _, _ = dispatch(Tool.ZOHO, {'備考': original[:255]}, original=original)
    assert not notion.upsert_calls
    _, notion, notes, result = dispatch(Tool.ZOHO, {'備考': original[:100]}, original=original)
    assert not notion.upsert_calls
    assert Tool.NOTION in result.properties[0].skipped_tools
    assert any('逆同期を保留' in v for v in notes[-1].values())
    _, notion, _, _ = dispatch(Tool.ZOHO, {'備考': '編集' + original[:253]}, original=original)
    assert notion.upsert_calls


def test_creation_plan_uses_same_limit_without_changing_source():
    from src.hub_creation.adapters import ZohoCreationAdapter
    class CreationClient(Client):
        def _request(self, method, path):
            if '/settings/fields?' in path:
                data = {'fields': FIELDS + [{'api_name': 'Account_Name', 'data_type': 'text', 'system_mandatory': True}, {'api_name': 'field4', 'data_type': 'text'}]}
            elif '/settings/layouts?' in path:
                data = {'layouts': [{'sections': []}]}
            else:
                data = {'data': [], 'info': {'more_records': False}}
            return SimpleNamespace(ok=True, status_code=200, json=lambda: data)
    props = {'取引先名': 'テスト法人', '備考': 'あ' * 299}
    client = CreationClient()
    adapter = ZohoCreationAdapter(client)
    payload = adapter.plan('client_master', props)
    assert payload['field12'] == 'あ' * 255
    assert any(payload.text_limit_notes.values())
    assert len(props['備考']) == 299
    assert adapter.create('client_master', payload) == 'z1'
    assert client.writes[0]['field12'] == 'あ' * 255


def test_project_update_keeps_managed_block_and_echo_compares_available_body():
    client = Client()
    client.fields = [{'api_name': 'field70', 'data_type': 'textarea', 'length': 255}]
    client.record['field70'] = merge_choice_memo('旧本文', 'サイトコントローラー', ['リンカーン'], limit=None)
    target = ZohoSyncTarget(client, 'Deals')
    target.upsert_record('z1', {'メモ': 'あ' * 299}, db_key='project')
    body, block = split_controller_memo(client.writes[-1]['field70'])
    assert 'リンカーン' in block
    assert len(client.writes[-1]['field70']) == 255
    assert target.is_shortened_echo('メモ', body, 'あ' * 299, db_key='project', external_id='z1')
    assert not target.is_shortened_echo('メモ', body[:10], 'あ' * 299, db_key='project', external_id='z1')


def test_controller_only_resize_of_full_memo_stops_without_writing():
    client = Client()
    client.fields = [{'api_name': 'field70', 'data_type': 'textarea', 'length': 255}]
    client.record['field70'] = shorten(merge_choice_memo('あ' * 299, 'サイトコントローラー', ['リンカーン'], limit=None), 255, managed=True)
    with pytest.raises(ApiError, match='本文も同時に再送'):
        ZohoSyncTarget(client, 'Deals').upsert_record('z1', {'サイトコントローラー': ['リンカーン', 'ねっぱん']}, db_key='project')
    assert not client.writes


def test_note_category_roundtrips_without_claiming_unresolved():
    from src.sync_engine.sync_notes import render_notes, parse_notes, TEXT_LIMITS
    notes = limit_payload('client_master', {'field12': 'あ' * 299}, FIELDS).text_limit_notes
    assert all(key.startswith(TEXT_LIMITS) for key in notes)
    assert parse_notes(render_notes(notes)) == notes


def test_creation_service_records_plan_before_post_and_success_after():
    from tests.hub_creation.test_service import setup
    from src.sync_engine.zoho_text_limits import TextLimitedPayload
    service, event, journal, notion, adapter, store = setup()
    limited = limit_payload('chain', {'field6': 'あ' * 299}, [{'api_name': 'field6', 'data_type': 'textarea', 'length': 255}])
    adapter.plan = lambda db, props: TextLimitedPayload(props, limited.text_limit_notes, limited.planned_text_limit_notes)
    create = adapter.create
    def checked_create(db, payload):
        assert any('送信する予定です' in v for v in notion.notes[-1][1].values())
        return create(db, payload)
    adapter.create = checked_create
    service.handle(event)
    assert any('送信しました' in v for v in notion.notes[-1][1].values())


def test_new_success_note_replaces_previous_failure_note_without_body_values():
    value = 'あ' * 299
    _, _, notes, _ = dispatch(Tool.NOTION, {'備考': value}, original=value)
    assert value not in str(notes)
    _, _, notes, _ = dispatch(Tool.NOTION, {'備考': '短い本文'}, original='短い本文')
    assert notes and not any(notes[-1].values())


def test_other_source_does_not_adopt_newer_zoho_shortened_candidate():
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    original = 'あ' * 299
    client = Client()
    client.record.update({'field12': original[:255], 'updated_at': now + timedelta(hours=1)})
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key='p1', db_key='client_master', zoho_id='z1', kintone_id='k1',
                           last_synced_at=now - timedelta(days=1)), expected_last_synced_at=None)
    notion = FakeSyncTarget(Tool.NOTION, {'p1': {'備考': original, NOTION_LAST_EDITED_TIME_KEY: now}})
    Dispatcher(store, {Tool.NOTION: notion, Tool.ZOHO: ZohoSyncTarget(client, 'Accounts')}).dispatch(
        SyncEvent(Tool.KINTONE, 'client_master', 'k1', now, {'備考': original}))
    assert all(values.get('備考', original) == original for _, values in notion.upsert_calls)


@pytest.mark.parametrize('body', [{}, {'fields': []}, {'fields': [None]}, {'fields': 'invalid'}])
def test_http_metadata_rejects_malformed_response(body):
    from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError
    client = HttpZohoClient(client_id='test', client_secret='test', refresh_token='test')
    client._request = lambda *args, **kwargs: SimpleNamespace(ok=True, status_code=200, json=lambda: body)
    with pytest.raises(ZohoApiError, match='文字数上限'):
        client.get_fields('Accounts')


@pytest.mark.parametrize('adopted', ['新しい本文', 'い' * 299])
def test_kintone_edit_is_delivered_to_excluded_zoho_projection(adopted):
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    original = 'あ' * 299
    client = Client()
    client.record.update({'field12': original[:255], 'updated_at': now + timedelta(hours=1)})
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key='p1', db_key='client_master', zoho_id='z1', kintone_id='k1',
                           last_synced_at=now - timedelta(days=1)), expected_last_synced_at=None)
    notion = FakeSyncTarget(Tool.NOTION, {'p1': {'備考': original, NOTION_LAST_EDITED_TIME_KEY: now - timedelta(hours=1)}})
    result = Dispatcher(store, {Tool.NOTION: notion, Tool.ZOHO: ZohoSyncTarget(client, 'Accounts')}).dispatch(
        SyncEvent(Tool.KINTONE, 'client_master', 'k1', now, {'備考': adopted}))
    assert notion.upsert_calls == [('p1', {'備考': adopted})]
    assert client.writes == [{'field12': adopted[:255]}]
    assert Tool.ZOHO in result.properties[0].written_tools


def test_no_op_projection_needs_no_extra_zoho_write():
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    original = 'あ' * 299
    client = Client()
    client.record.update({'field12': original[:255], 'updated_at': now + timedelta(hours=1)})
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key='p1', db_key='client_master', zoho_id='z1', kintone_id='k1',
                           last_synced_at=now - timedelta(days=1)), expected_last_synced_at=None)
    notion = FakeSyncTarget(Tool.NOTION, {'p1': {'備考': original, NOTION_LAST_EDITED_TIME_KEY: now}})
    result = Dispatcher(store, {Tool.NOTION: notion, Tool.ZOHO: ZohoSyncTarget(client, 'Accounts')}).dispatch(
        SyncEvent(Tool.KINTONE, 'client_master', 'k1', now, {'備考': original}))
    assert not notion.upsert_calls
    assert not client.writes

    assert not result.properties[0].skipped_tools


def test_accepted_zoho_edit_clears_omission_note_but_echo_keeps_it():
    from src.sync_engine.zoho_text_limits import text_limit_key
    original = 'あ' * 299
    _, notion, notes, _ = dispatch(Tool.ZOHO, {'備考': '編集済み'}, original=original)
    assert notion.upsert_calls
    assert notes[-1][text_limit_key('備考')] == ''
    _, notion, notes, _ = dispatch(Tool.ZOHO, {'備考': original[:255]}, original=original)
    assert not notion.upsert_calls
    assert not notes


def test_creation_notes_resume_without_recreate_and_do_not_revert_later_notes():
    from tests.hub_creation.test_service import setup
    from src.sync_engine.zoho_text_limits import TextLimitedPayload, creation_notes_pending
    service, event, journal, notion, adapter, store = setup()
    limited = limit_payload('chain', {'field6': 'あ' * 299}, [{'api_name': 'field6', 'data_type': 'textarea', 'length': 255}])
    adapter.plan = lambda db, props: TextLimitedPayload(props, limited.text_limit_notes, limited.planned_text_limit_notes)
    write = notion.upsert_sync_notes
    def fail_success(page, notes):
        if any('送信しました' in value for value in notes.values()):
            raise RuntimeError('メモ保存失敗')
        write(page, notes)
    notion.upsert_sync_notes = fail_success
    with pytest.raises(RuntimeError, match='メモ保存失敗'):
        service.handle(event)
    assert store.get('page').zoho_id == 'external-1'
    assert adapter.creates == 1
    assert creation_notes_pending(journal.get('notion:page', 'zoho'))
    notion.upsert_sync_notes = write
    assert service.handle(event) == 'hub_creation_complete'
    assert adapter.creates == 1
    assert any('送信しました' in v for v in notion.notes[-1][1].values())
    assert not creation_notes_pending(journal.get('notion:page', 'zoho'))
    notion.notes.clear()
    assert service.handle(event) is None
    assert notion.notes == []


def test_legacy_created_reason_is_explicitly_unknown_when_resuming():
    from src.sync_engine.zoho_text_limits import recovered_creation_notes
    notes = recovered_creation_notes({'state': 'created', 'reason': ''}, 'chain', {'メモ': '原文'})
    assert any('登録済み。省略結果の記録は未確認' in v for v in notes.values())


def test_controller_only_update_records_new_shortening():
    from src.sync_engine.zoho_text_limits import collect_text_limit_notes, collected_notes
    client = Client()
    client.fields = [{'api_name': 'field70', 'data_type': 'textarea', 'length': 255}]
    client.record['field70'] = shorten(merge_choice_memo('あ' * 299, 'サイトコントローラー', ['なし'], limit=None), 250, managed=True)
    with collect_text_limit_notes():
        ZohoSyncTarget(client, 'Deals').upsert_record('z1', {'サイトコントローラー': ['リンカーン', 'ねっぱん']}, db_key='project')
        assert any('省略して送信しました' in v for v in collected_notes().values())
    assert len(client.writes[-1]['field70']) == 255


def test_oversized_managed_block_is_an_api_error():
    client = Client()
    client.fields = [{'api_name': 'field70', 'data_type': 'textarea', 'length': 10}]
    with pytest.raises(ApiError, match='管理用メモ') as caught:
        ZohoSyncTarget(client, 'Deals').upsert_record('z1', {'メモ': '本文', 'サイトコントローラー': ['ねっぱん']}, db_key='project')
    assert caught.value.status_code == 422
    assert not client.writes


def test_creation_limit_failure_becomes_creation_hold(monkeypatch):
    from tests.hub_creation.test_adapters import Client as CreationClient
    from src.hub_creation.adapters import ZohoCreationAdapter
    from src.hub_creation.domain import CreationHeld
    from src.sync_engine import zoho_text_limits
    def fail(*args, **kwargs):
        raise ValueError('管理用メモだけで文字数上限を超えるため保持します')
    monkeypatch.setattr(zoho_text_limits, 'limit_payload', fail)
    with pytest.raises(CreationHeld, match='管理用メモ'):
        ZohoCreationAdapter(CreationClient()).plan('chain', {'グループ名': 'テスト'})


def test_metadata_cache_is_one_dispatch_only():
    from src.sync_engine.zoho_text_limits import collect_text_limit_notes
    client = Client()
    target = ZohoSyncTarget(client, 'Accounts')
    with collect_text_limit_notes():
        assert target.is_shortened_echo('備考', 'あ' * 255, 'あ' * 299, db_key='client_master')
        assert target.is_shortened_echo('備考', 'あ' * 255, 'あ' * 299, db_key='client_master')
        target.upsert_record('z1', {'備考': 'あ' * 299}, db_key='client_master')
    assert client.reads == 1
    client.fields = [{'api_name': 'field12', 'data_type': 'text', 'length': 500}]
    with collect_text_limit_notes():
        target.upsert_record('z1', {'備考': 'あ' * 299}, db_key='client_master')
    assert client.reads == 2
    assert client.writes[-1]['field12'] == 'あ' * 299


@pytest.mark.parametrize('new_limit', [128, 500])
def test_limit_change_holds_old_shortened_echo(new_limit):
    client = Client()
    client.fields = [{'api_name': 'field12', 'data_type': 'text', 'length': new_limit}]
    _, notion, notes, result = dispatch(Tool.ZOHO, {'備考': 'あ' * 255}, client=client, original='あ' * 299)
    assert not notion.upsert_calls
    assert Tool.NOTION in result.properties[0].skipped_tools
    assert any('逆同期を保留' in v for v in notes[-1].values())


def test_limit_change_does_not_make_old_projection_a_winning_candidate():
    now = datetime(2026, 10, 16, tzinfo=timezone.utc)
    original = 'あ' * 299
    client = Client()
    client.fields = [{'api_name': 'field12', 'data_type': 'text', 'length': 500}]
    client.record.update({'field12': original[:255], 'updated_at': now + timedelta(hours=1)})
    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key='p1', db_key='client_master', zoho_id='z1', kintone_id='k1',
                           last_synced_at=now - timedelta(days=1)), expected_last_synced_at=None)
    notion = FakeSyncTarget(Tool.NOTION, {'p1': {'備考': original, NOTION_LAST_EDITED_TIME_KEY: now}})
    result = Dispatcher(store, {Tool.NOTION: notion, Tool.ZOHO: ZohoSyncTarget(client, 'Accounts')}).dispatch(
        SyncEvent(Tool.KINTONE, 'client_master', 'k1', now, {'備考': original}))
    assert not notion.upsert_calls
    assert client.writes == [{'field12': original}]
    assert Tool.ZOHO in result.properties[0].written_tools


def test_failed_reservation_withdraws_only_this_shortening_plan():
    from tests.hub_creation.test_service import setup
    from src.sync_engine.zoho_text_limits import TextLimitedPayload
    service, event, journal, notion, adapter, store = setup()
    limited = limit_payload('chain', {'field6': 'あ' * 299}, [{'api_name': 'field6', 'data_type': 'textarea', 'length': 255}])
    adapter.plan = lambda db, props: TextLimitedPayload(props, limited.text_limit_notes, limited.planned_text_limit_notes)
    journal.reserve = lambda *args, **kwargs: False
    assert service.handle(event) == 'hub_creation_held'
    assert adapter.creates == 0
    assert any('予定です' in v for v in notion.notes[0][1].values())
    assert notion.notes[1][1] == {key: '' for key in limited.text_limit_notes}


def test_router_normalizes_only_free_text_and_keeps_raw_version():
    from src.sync_engine.production_wiring import _MultiDbZohoSyncTarget
    client = Client()
    raw_memo = merge_choice_memo('本文', 'サイトコントローラー', ['ねっぱん'], limit=None)
    client.record.update({'field70': raw_memo, 'Deal_Name': '案件名'})
    router = _MultiDbZohoSyncTarget({'project': ZohoSyncTarget(client, 'Deals')})
    record = router.get_record('z1', db_key='project')
    assert record['メモ'] == '本文'
    assert record['field70'] == raw_memo
    assert record['Modified_Time'] == client.record['Modified_Time']
    assert '案件名' not in record


def test_notion_resolution_clears_ambiguity_only_after_success():
    from src.sync_engine.zoho_text_limits import ambiguity_note, ambiguity_key, collect_text_limit_notes, collected_notes
    from src.sync_engine.sync_notes import merge_notes
    held = ambiguity_note('備考')
    _, _, notes, _ = dispatch(Tool.NOTION, {'備考': '意図した本文'}, original='意図した本文')
    assert notes[-1][ambiguity_key('備考')] == ''
    assert ambiguity_key('備考') not in merge_notes(held, notes[-1])
    client = Client()
    client.fail = True
    with collect_text_limit_notes():
        with pytest.raises(ApiError):
            ZohoSyncTarget(client, 'Accounts').upsert_record('z1', {'備考': '意図した本文'}, db_key='client_master')
        assert merge_notes(held, collected_notes()) == held


def test_controller_only_success_does_not_clear_body_ambiguity():
    from src.sync_engine.zoho_text_limits import ambiguity_key, collect_text_limit_notes, collected_notes
    client = Client()
    client.fields = [{'api_name': 'field70', 'data_type': 'textarea', 'length': 255}]
    client.record['field70'] = '本文'
    with collect_text_limit_notes():
        ZohoSyncTarget(client, 'Deals').upsert_record('z1', {'サイトコントローラー': ['ねっぱん']}, db_key='project')
        assert ambiguity_key('メモ') not in collected_notes()
