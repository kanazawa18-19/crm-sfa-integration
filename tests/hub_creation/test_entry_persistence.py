"""架空の受信値から9経路の計画・予約・保存をつなぐ。外部通信は拒否する。"""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlparse
import json
import os
import uuid

import psycopg
from psycopg import sql
import pytest

from src.db_schema.registry import get_schema
from src.hub_creation.adapters import ZohoCreationAdapter, KintoneCreationAdapter
from src.hub_creation.journal import PostgresCreationJournal
from src.hub_creation.service import HubCreationService
from src.sync_engine.clients.notion_client import build_notion_properties
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.webhook_handlers.notion_webhook import notion_payload_to_sync_event
from src.sync_engine.webhook_handlers.spreadsheet_webhook import spreadsheet_payload_to_sync_event

PAGE = '11111111-1111-4111-8111-111111111111'
CLIENT = '22222222-2222-4222-8222-222222222222'
OWNER = '33333333-3333-4333-8333-333333333333'
NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
PROPERTIES = {
    'chain': {'グループ名': '架空グループ'},
    'client_master': {'取引先名': '架空取引先'},
    'contact': {'名前': '架空 花子', '姓': '架空', '名': '花子'},
    'project': {'案件名': '架空案件', '営業ステータス': '与件整理',
                '作成日': '2026-09-28', '完了予定日': '2026-10-01',
                '契約予定日': '2026-10-01', '担当メンバー': [OWNER]},
    'product': {'名前': '架空商品', '課金形態': '月額ストック'},
    'action': {'商談回数・電話回数・メール回数（何回目）': '架空連絡',
               'アクション種別': 'テレアポ', '履歴メモ': '架空メモ',
               '👨‍👩‍👧‍👦 取引先マスター': [CLIENT]},
}
ROUTES = [('zoho', key) for key in PROPERTIES] + [('kintone', key) for key in ('client_master', 'project', 'action')]


def response(data):
    return SimpleNamespace(ok=True, status_code=200, json=lambda: data)


@pytest.fixture
def journal(monkeypatch):
    dsn = os.environ.get('SYNC_REVIEW_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('明示した隔離PostgreSQLのみで実行')
    parsed = urlparse(dsn)
    assert parsed.scheme in {'postgresql', 'postgres'} and not parsed.fragment
    assert parsed.hostname in {'127.0.0.1', 'localhost'} and parsed.port == 55443
    assert parsed.path == '/crm_field_review_test' and not parsed.query
    schema = 'entry_' + uuid.uuid4().hex
    scoped = dsn + '?options=' + quote('-csearch_path=' + schema)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    monkeypatch.setenv('DATABASE_URL', scoped)
    monkeypatch.setenv('DATABASE_URL_UNPOOLED', scoped)
    # APIクライアントの実通信は、認証情報が環境にあっても許可しない。
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **k: pytest.fail('外部HTTPは禁止'))
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute(Path('dashboard/prisma/migrations/20260928000000_add_hub_creation_attempt/migration.sql').read_text())
        yield PostgresCreationJournal()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


class NotionFixture:
    def __init__(self, db, properties):
        self.db, self.properties = db, properties
        self.creates, self.notes = 0, []

    def get_raw_page(self, page):
        raw = build_notion_properties(self.properties, get_schema(self.db))
        for value in raw.values():
            value['type'] = next(iter(value))
            if value['type'] in {'title', 'rich_text'}:
                for text in value[value['type']]:
                    text['plain_text'] = text['text']['content']
        if self.db == 'action':
            raw['担当営業'] = {'type': 'rollup', 'rollup': {'type': 'array', 'array': [
                {'type': 'people', 'people': [{'id': OWNER}]}]}}
            raw['提案サービス'] = {'type': 'rollup', 'rollup': {'type': 'array', 'array': [
                {'type': 'multi_select', 'multi_select': [{'name': 'メイリー'}]}]}}
        return {'id': page, 'created_time': NOW.isoformat(),
                'parent': {'database_id': get_schema(self.db).notion_database_id}, 'properties': raw}

    def query_raw(self, body):
        return {'results': [], 'has_more': False}

    def create_page_once(self, properties):
        self.creates += 1
        self.properties = deepcopy(properties)
        return PAGE

    def upsert_sync_notes(self, page, notes):
        self.notes.append(notes)


@pytest.mark.parametrize('target,db', ROUTES)
@pytest.mark.parametrize('origin', ['notion', 'spreadsheet'])
@pytest.mark.parametrize('post_timeout', [False, True], ids=['成功', '応答不明'])
def test_entry_plan_persist_and_redelivery(target, db, origin, post_timeout, journal, monkeypatch, tmp_path):
    """9経路×2入口で、再通知が成功済み/応答不明のPOSTを増やさない。"""
    monkeypatch.setenv('CRM_USER_MAPPING_JSON', json.dumps({'verified': True, 'users': [
        {'notion_id': OWNER, 'zoho_id': '900', 'kintone_code': 'fixture-user', 'enabled': True}]}))
    store = SQLiteIdMappingStore(str(tmp_path / 'mapping.sqlite'))
    store.upsert(IdMapping(notion_key=CLIENT, db_key='client_master', zoho_id='901', kintone_id='902'))
    properties = deepcopy(PROPERTIES[db])
    notion = NotionFixture(db, properties)
    posts = []

    def create(*args):
        posts.append(args)
        if post_timeout:
            raise TimeoutError('架空の応答不明')
        return '999'

    if target == 'zoho':
        # 対応付け単体では既存テストを利用し、ここでは受信値が実アダプターへ届く境界を調べる。
        types = {'Name': 'text', 'Account_Name': 'text', 'field4': 'text', 'Last_Name': 'text',
                 'First_Name': 'text', 'Deal_Name': 'text', 'Stage': 'picklist', 'field42': 'date',
                 'Closing_Date': 'date', 'field51': 'date', 'Owner': 'ownerlookup',
                 'Product_Name': 'text', 'field2': 'text', 'field': 'textarea', 'field7': 'text',
                 'field6': 'lookup', 'field15': 'picklist'}
        def get(method, path):
            assert method == 'GET'
            if '/settings/fields?' in path:
                return response({'fields': [{'api_name': code, 'data_type': kind} for code, kind in types.items()]})
            if '/settings/layouts?' in path:
                return response({'layouts': [{'sections': [{'fields': []}]}]})
            return response({'data': [], 'info': {'more_records': False}})
        adapter = ZohoCreationAdapter(SimpleNamespace(_request=get, insert_record=create), store)
    else:
        fields = {'顧客名': {'type': 'SINGLE_LINE_TEXT', 'unique': True},
                  '店舗名': {'type': 'SINGLE_LINE_TEXT'}, '日付_0': {'type': 'DATE'},
                  '日付': {'type': 'DATE'}, '営業担当者': {'type': 'USER_SELECT'},
                  'comment': {'type': 'MULTI_LINE_TEXT'}, 'cnctorMember': {'type': 'USER_SELECT'},
                  'service': {'type': 'MULTI_SELECT', 'options': {'メイリー': {}}},
                  'client_name': {'type': 'SINGLE_LINE_TEXT'}}
        def request(method, url, **kwargs):
            assert method == 'GET' and url.startswith('https://fixture.invalid/')
            return response({'properties': fields} if 'fields.json' in url else {'records': []})
        monkeypatch.setattr('src.hub_creation.adapters.request_with_retry', request)
        client = SimpleNamespace(_domain='fixture.invalid', _headers=lambda **k: {}, _timeout=1,
                                 _max_retries=0, _backoff_base=0, add_record=create,
                                 get_record=lambda *a: {'顧客名': '架空取引先'})
        adapter = KintoneCreationAdapter({key: SimpleNamespace(_client=client, _app='1')
                                          for key in ('client_master', 'project', 'action')}, store)
    sheet_updates = []
    sheet = SimpleNamespace(read=lambda *a, **k: (deepcopy(properties), 12, 9),
                            update=lambda *a, **k: sheet_updates.append(k) or 9)
    service = HubCreationService(store=store, journal=journal, notion_clients={db: notion},
                                 adapters=[adapter], enabled_since=NOW, sheet_gateway=sheet)
    if origin == 'notion':
        event = notion_payload_to_sync_event({'database_id': get_schema(db).notion_database_id,
            'page_id': PAGE, 'last_edited_time': NOW.isoformat(), 'properties': notion.get_raw_page(PAGE)['properties']}, {})
        key = 'notion:' + PAGE
    else:
        registration = 'new:' + PAGE
        event = spreadsheet_payload_to_sync_event({'sheet': get_schema(db).spreadsheet_sheet_name,
            'row': 9, 'editedAt': NOW.isoformat(), 'action': 'register_new', 'values': {'同期キー': registration}}, {})
        key = 'sheet:' + registration
    result = service.handle(event)
    assert result == ('hub_creation_held' if post_timeout else 'hub_creation_complete'), notion.notes
    assert len(posts) == 1
    expected = {
        ('zoho', 'chain'): {'Name': '架空グループ'},
        ('zoho', 'client_master'): {'Account_Name': '架空取引先', 'field4': '架空取引先'},
        ('zoho', 'contact'): {'Last_Name': '架空', 'First_Name': '花子'},
        ('zoho', 'project'): {'Deal_Name': '架空案件', 'Closing_Date': '2026-10-01', 'Owner': {'id': '900'}},
        ('zoho', 'product'): {'Product_Name': '架空商品', 'field15': 'ランニング'},
        ('zoho', 'action'): {'Name': '架空連絡', 'field': '架空メモ', 'field6': {'id': '901'}},
        ('kintone', 'client_master'): {'顧客名': '架空取引先'},
        ('kintone', 'project'): {'店舗名': '架空案件', '日付_0': '2026-09-28', '営業担当者': [{'code': 'fixture-user'}]},
        ('kintone', 'action'): {'comment': '架空メモ', 'client_name': '架空取引先', 'cnctorMember': [{'code': 'fixture-user'}], 'service': ['メイリー']},
    }[target, db]
    assert posts[0][0] == (get_schema(db).zoho_api_module if target == 'zoho' else '1')
    assert expected.items() <= posts[0][1].items()
    # 登録履歴を新しいインスタンスで実DBを再読込し、再通知時の予約消失を検出する。
    service.journal = PostgresCreationJournal()
    service.handle(event)
    assert len(posts) == 1
    row = service.journal.get(key, target)
    assert row['state'] == ('reserved' if post_timeout else 'created')
    mapping = store.get(PAGE)
    assert getattr(mapping, target + '_id') == (None if post_timeout else '999')
    if origin == 'spreadsheet':
        assert notion.creates == 1 and mapping.spreadsheet_row == 9
        assert sheet_updates[-1]['notion_key'] == PAGE
        assert ('外部登録完了' in sheet_updates[-1]['status']) is (not post_timeout)
        if post_timeout:
            assert '作成結果が不明' in sheet_updates[-1]['status']
        assert service.journal.get(key, 'notion')['state'] == 'created'
    else:
        assert notion.creates == 0
    store.close()
