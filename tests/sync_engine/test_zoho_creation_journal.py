"""Zoho取込は作成結果不明でも再POST・推測紐付け・自動削除をしない。"""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.production_wiring import _MultiDbNotionSyncTarget
from src.sync_engine.sync_event import SyncEvent
from tests.hub_creation.test_service import Journal, setup as hub_setup
from tests.sync_engine.test_dispatcher import FakeSyncTarget, NOW, _zoho_project_record


class SingleCreateTarget(FakeSyncTarget):
    def __init__(self):
        super().__init__(Tool.NOTION, {'new-id': {'案件名': '検証'}})
        self.create_error = None
        self.creates = 0

    def created_record_available(self, page_id, *, db_key):
        return page_id in self._records

    def create_record_once(self, properties, *, db_key):
        self.creates += 1
        if self.create_error:
            raise self.create_error
        return 'new-id'


def setup(monkeypatch):
    monkeypatch.setenv('AUTO_CREATE_NEW_RECORDS_ENABLED', 'true')
    monkeypatch.setattr('src.sync_engine.dispatcher.time.sleep', lambda _: None)
    source = FakeSyncTarget(Tool.ZOHO,
                            {'source': _zoho_project_record()})
    notion, journal, store = SingleCreateTarget(), Journal(), SQLiteIdMappingStore()
    dispatcher = Dispatcher(store, {Tool.ZOHO: source, Tool.NOTION: notion}, creation_journal=journal)
    event = SyncEvent(Tool.ZOHO, 'project', 'source', NOW, {})
    return dispatcher, event, notion, journal, store


def test_通信結果不明は別通知でも再作成しない(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    notion.create_error = TimeoutError('結果不明')
    assert dispatcher.dispatch(event).reason == 'new_record_creation_status_unknown'
    assert dispatcher.dispatch(replace(event, occurred_at=NOW + timedelta(days=1))).reason == 'new_record_creation_status_unknown'
    assert notion.creates == 1
    assert journal.get('external:zoho:project:source', 'notion')['state'] == 'reserved'
    assert store.get('new-id') is None


def test_作成成功後に台帳ID保存失敗でも再作成しない(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    monkeypatch.setattr(journal, 'finish', Mock(side_effect=RuntimeError('DB断')))
    assert dispatcher.dispatch(event).reason == 'new_record_creation_status_unknown'
    assert dispatcher.dispatch(replace(event, occurred_at=NOW + timedelta(days=1))).reason == 'new_record_creation_status_unknown'
    assert notion.creates == 1
    assert notion.delete_calls == []


def test_対応表保存失敗は確定IDを保持して復旧する(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    register = dispatcher._register_new_record_mapping
    monkeypatch.setattr(dispatcher, '_register_new_record_mapping', lambda _: RuntimeError('保存失敗'))
    assert dispatcher.dispatch(event).reason == 'new_record_mapping_registration_failed'
    assert journal.get('external:zoho:project:source', 'notion')['externalId'] == 'new-id'
    assert journal.get('external:zoho:project:source', 'mapping:notion')['state'] == 'blocked'
    assert notion.delete_calls == []
    monkeypatch.setattr(dispatcher, '_register_new_record_mapping', register)
    dispatcher.dispatch(replace(event, occurred_at=NOW + timedelta(days=1)))
    assert store.get('new-id').zoho_id == 'source'
    assert journal.get('external:zoho:project:source', 'mapping:notion') is None
    assert notion.creates == 1
    assert notion.delete_calls == []


def test_対応表が保存済みで確認読取だけ壊れてもarchiveしない(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    def uncertain(mapping):
        store.upsert(mapping)
        return RuntimeError('応答消失と読取障害')
    monkeypatch.setattr(dispatcher, '_register_new_record_mapping', uncertain)
    assert dispatcher.dispatch(event).reason == 'new_record_mapping_registration_failed'
    assert store.get('new-id').zoho_id == 'source'
    assert notion.delete_calls == []
    dispatcher.dispatch(replace(event, occurred_at=NOW + timedelta(days=1)))
    assert notion.creates == 1


def test_台帳未設定は作成しない(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    dispatcher._creation_journal = None
    assert dispatcher.dispatch(event).reason == 'new_record_creation_journal_unavailable'
    assert notion.creates == 0


def test_予約失敗は作成しない(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    monkeypatch.setattr(journal, 'reserve', Mock(side_effect=RuntimeError('DB断')))
    with pytest.raises(RuntimeError, match='DB断'):
        dispatcher.dispatch(event)
    assert notion.creates == 0


def test_本番対象は同名推測付き作成を呼ばない():
    client = Mock()
    client.create_page_once.return_value = 'page'
    target = _MultiDbNotionSyncTarget({'project': client}, SQLiteIdMappingStore())
    assert target.create_record_once({'案件名': '同名'}, db_key='project') == 'page'
    client.create_page_once.assert_called_once_with({'案件名': '同名'})
    client.create_page.assert_not_called()


@pytest.mark.parametrize('state', ['reserved', 'created'])
def test_取込の折返しで別Zohoレコードを作らない(state):
    service, event, journal, notion, adapter, store = hub_setup()
    journal.rows['external:zoho:chain:source', 'notion'] = {
        'state': state, 'externalId': event.external_id if state == 'created' else None,
    }
    expected = 'external_notion_import_pending' if state == 'created' else 'external_notion_import_database_pending'
    assert service.handle(event) == expected
    assert adapter.creates == 0


def test_新しい処理インスタンスでも予約を再利用する(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    notion.create_error = TimeoutError('結果不明')
    dispatcher.dispatch(event)
    restarted = Dispatcher(store, dispatcher._targets, creation_journal=journal)
    assert restarted.dispatch(event).reason == 'new_record_creation_status_unknown'
    assert notion.creates == 1


def test_実際の対応表保存障害後は同じページを再利用する(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    upsert = store.upsert
    monkeypatch.setattr(store, 'upsert', Mock(side_effect=RuntimeError('保存障害')))
    assert dispatcher.dispatch(event).reason == 'new_record_mapping_registration_failed'
    assert notion.delete_calls == []
    monkeypatch.setattr(store, 'upsert', upsert)
    dispatcher.dispatch(event)
    assert store.get('new-id').zoho_id == 'source'
    assert notion.creates == 1


def test_復旧先が消えていても再作成しない(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    journal.rows['external:zoho:project:source', 'notion'] = {'state': 'created', 'externalId': 'gone'}
    assert dispatcher.dispatch(event).reason == 'new_record_created_page_unavailable'
    assert notion.creates == 0


def test_復旧時も既存の他連携IDを維持する(monkeypatch):
    from src.sync_engine.id_mapping import IdMapping
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    journal.rows['external:zoho:project:source', 'notion'] = {'state': 'created', 'externalId': 'new-id'}
    store.upsert(IdMapping(notion_key='new-id', db_key='project', kintone_id='existing', spreadsheet_row=42, last_synced_at=NOW))
    dispatcher.dispatch(event)
    saved = store.get('new-id')
    assert (saved.zoho_id, saved.kintone_id, saved.spreadsheet_row) == ('source', 'existing', 42)
    assert notion.creates == 0


@pytest.mark.parametrize('page', [
    {'archived': True, 'parent': {'database_id': 'other'}},
    {'in_trash': True, 'parent': {'database_id': 'other'}},
    {'parent': {'database_id': 'other'}},
])
def test_本番復旧先は有効な同DBページに限る(page):
    client = Mock()
    client.get_raw_page.return_value = page
    target = _MultiDbNotionSyncTarget({'project': client}, SQLiteIdMappingStore())
    assert not target.created_record_available('page', db_key='project')


def test_本番復旧先の元DBを確認する():
    from src.db_schema.registry import get_schema
    client = Mock()
    client.get_raw_page.return_value = {'parent': {'database_id': get_schema('project').notion_database_id}}
    target = _MultiDbNotionSyncTarget({'project': client}, SQLiteIdMappingStore())
    assert target.created_record_available('page', db_key='project')


def test_別連携IDだけ一致してもZoho対応保存成功とみなさない(monkeypatch):
    from src.sync_engine.id_mapping import IdMapping
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    store.upsert(IdMapping(notion_key='new-id', db_key='project', kintone_id='existing', last_synced_at=NOW))
    intended = replace(store.get('new-id'), zoho_id='source')
    assert not dispatcher._mapping_was_actually_registered(intended)


def test_保存応答消失で保留になっても次通知で表示を解消する(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    def uncertain(mapping):
        store.upsert(mapping)
        return RuntimeError('応答消失')
    monkeypatch.setattr(dispatcher, '_register_new_record_mapping', uncertain)
    dispatcher.dispatch(event)
    assert journal.get('external:zoho:project:source', 'mapping:notion')['state'] == 'blocked'
    dispatcher.dispatch(replace(event, occurred_at=NOW + timedelta(days=1)))
    assert journal.get('external:zoho:project:source', 'mapping:notion') is None
    assert notion.creates == 1


def test_保留表示の解消障害でも新規成功後の処理を続ける(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    monkeypatch.setattr(journal, 'dismiss_hold', Mock(side_effect=RuntimeError('DB断')))
    linked = []
    dispatcher._project_linker = lambda mapping: linked.append(mapping.notion_key) or ()
    assert not dispatcher.dispatch(event).skipped
    assert linked == ['new-id']
    assert store.get('new-id').zoho_id == 'source'


def test_復旧失敗のページ無効を監視保留に残す(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    journal.rows['external:zoho:project:source', 'notion'] = {'state': 'created', 'externalId': 'gone'}
    assert dispatcher.dispatch(event).reason == 'new_record_created_page_unavailable'
    assert journal.get('external:zoho:project:source', 'mapping:notion')['state'] == 'blocked'


def test_復旧先の別Zoho対応を監視保留に残す(monkeypatch):
    from src.sync_engine.id_mapping import IdMapping
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    journal.rows['external:zoho:project:source', 'notion'] = {'state': 'created', 'externalId': 'new-id'}
    store.upsert(IdMapping(notion_key='new-id', db_key='project', zoho_id='another-source'))
    assert dispatcher.dispatch(event).reason == 'new_record_created_page_mapping_conflict'
    assert journal.get('external:zoho:project:source', 'mapping:notion')['state'] == 'blocked'
    assert store.get('new-id').zoho_id == 'another-source'


def test_台帳と対応表が異なると通常更新でも保留を解除しない(monkeypatch):
    from src.sync_engine.id_mapping import IdMapping
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    journal.rows['external:zoho:project:source', 'notion'] = {'state': 'created', 'externalId': 'another-page'}
    store.upsert(IdMapping(notion_key='new-id', db_key='project', zoho_id='source'))
    dispatcher.dispatch(event)
    assert journal.get('external:zoho:project:source', 'mapping:notion')['state'] == 'blocked'


def test_復旧直後の外部ID検索遅延でも作成ロックへ再入しない(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    journal.rows['external:zoho:project:source', 'notion'] = {'state': 'created', 'externalId': 'new-id'}
    monkeypatch.setattr(store, 'find_by_external_id', lambda *a, **k: None)
    dispatcher.dispatch(event)
    assert store.get('new-id').zoho_id == 'source'
    assert notion.creates == 0
    assert journal.get('external:zoho:project:source', 'mapping:notion') is None


def test_復旧直後のページキー検索遅延は再作成せず監視に残す(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    journal.rows['external:zoho:project:source', 'notion'] = {'state': 'created', 'externalId': 'new-id'}
    monkeypatch.setattr(store, 'get', lambda *a: None)
    assert dispatcher.dispatch(event).reason == 'unknown_record'
    assert notion.creates == 0
    assert journal.get('external:zoho:project:source', 'mapping:notion')['state'] == 'blocked'


def test_作成ID台帳保存失敗の照合IDを診断に残す(monkeypatch, caplog):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    monkeypatch.setattr(journal, 'finish', Mock(side_effect=RuntimeError('秘密のエラー本文')))
    assert dispatcher.dispatch(event).reason == 'new_record_creation_status_unknown'
    assert journal.get('external:zoho:project:source', 'notion')['state'] == 'reserved'
    assert 'new-id' in journal.get('external:zoho:project:source', 'mapping:notion')['reason']
    assert 'new-id' in caplog.text
    assert '秘密のエラー本文' not in caplog.text


def test_復旧先の確認通信障害を監視保留に残す(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    journal.rows['external:zoho:project:source', 'notion'] = {'state': 'created', 'externalId': 'new-id'}
    monkeypatch.setattr(notion, 'created_record_available', Mock(side_effect=TimeoutError('通信障害')))
    assert dispatcher.dispatch(event).reason == 'new_record_created_page_verification_pending'
    assert journal.get('external:zoho:project:source', 'mapping:notion')['state'] == 'blocked'
    assert journal.get('external:zoho:project:source', 'notion')['externalId'] == 'new-id'
    assert notion.creates == 0
    assert notion.delete_calls == []


def test_POST失敗は予約を維持して分類を保存する(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    notion.create_error = TimeoutError('結果不明')
    assert dispatcher.dispatch(event).reason == 'new_record_creation_status_unknown'
    row = journal.get('external:zoho:project:source', 'notion')
    assert row['state'] == 'reserved'
    assert row['reason']
    assert notion.creates == 1


def test_POST失敗の診断保存も失敗しても予約を維持する(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    notion.create_error = TimeoutError('結果不明')
    monkeypatch.setattr(journal, 'record_creation_failure', Mock(side_effect=RuntimeError('DB断')))
    assert dispatcher.dispatch(event).reason == 'new_record_creation_status_unknown'
    assert dispatcher.dispatch(event).reason == 'new_record_creation_status_unknown'
    assert journal.get('external:zoho:project:source', 'notion')['state'] == 'reserved'
    assert notion.creates == 1


def test_ID確定直後のプロセス停止でも監視保留がある(monkeypatch):
    dispatcher, event, notion, journal, store = setup(monkeypatch)
    monkeypatch.setattr(dispatcher, '_register_new_record_mapping', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        dispatcher.dispatch(event)
    assert journal.get('external:zoho:project:source', 'notion')['state'] == 'created'
    assert journal.get('external:zoho:project:source', 'mapping:notion')['state'] == 'blocked'
    assert notion.creates == 1
