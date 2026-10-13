"""問い合わせの保存後失敗・入力不足・差分限定を隔離DBで確認する。"""
from types import SimpleNamespace
from pathlib import Path
import os
from urllib.parse import urlparse
from uuid import uuid4
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from src.hub_creation import inquiry_handoff as inquiry
from src.hub_creation.journal import PostgresCreationJournal


@pytest.fixture
def local(monkeypatch):
    dsn = os.environ.get('SYNC_REVIEW_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('明示したローカルDBのみ')
    assert urlparse(dsn).hostname in {'127.0.0.1', 'localhost'}
    assert urlparse(dsn).path == '/crm_field_review_test'
    schema = 'inquiry_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f'-csearch_path={schema}')
    monkeypatch.setattr(inquiry, 'connect', connect)
    monkeypatch.setattr('src.hub_creation.journal.connect', connect)
    with connect() as conn:
        for name in ['20260928000000_add_hub_creation_attempt', '20261014010000_inquiry_handoff']:
            conn.execute((Path('dashboard/prisma/migrations') / name / 'migration.sql').read_text())
    yield connect
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def session(mapping=None, dispatch=None):
    wiring = SimpleNamespace(id_mapping_store=SimpleNamespace(get=lambda page: mapping),
                             dispatcher=SimpleNamespace(dispatch=dispatch))
    return inquiry.InquirySession(inquiry.InquiryHandoff(wiring=wiring), 'inquiry:test')


def client(props):
    return SimpleNamespace(get_page=lambda page: dict(props),
        get_raw_page=lambda page: {'last_edited_time': '2026-10-14T00:00:00Z'})


def test_会社なしは名前があっても作成を呼ばず既存台帳へ保留(local):
    s = session(dispatch=lambda event: pytest.fail('外部配送した'))
    props = {'名前': '名前だけ', 'メールアドレス': 'sample@example.com'}
    s.stage('page', props)
    result = s.deliver(client(props), 'page')
    assert result['state'] == 'blocked' and '取引先' in result['reason']
    assert PostgresCreationJournal().get('notion:page', 'zoho')['state'] == 'blocked'


def test_会社ありでも明示姓がなければ保留(local):
    s = session(dispatch=lambda event: pytest.fail('外部配送した'))
    props = {'名前': '名前だけ', '取引先マスター': ['company']}
    s.stage('page', props)
    assert '姓' in s.deliver(client(props), 'page')['reason']


def test_保存後の配送失敗を空差分再送で修復し4項目外を渡さない(local):
    events = []
    def failed(event):
        raise RuntimeError('通信失敗')
    s = session(mapping=SimpleNamespace(zoho_id='zoho'), dispatch=failed)
    props = {'携帯番号': '09000000000'}
    s.stage('page', props)
    current = client(dict(props, 名前='既存', 部署='同期しない'))
    with pytest.raises(RuntimeError):
        s.deliver(current, 'page')
    s.stage('page', {})
    s.flow.wiring.dispatcher.dispatch = lambda event: events.append(event) or SimpleNamespace(skipped=False, has_partial_skips=False)
    assert s.deliver(current, 'page')['state'] == 'done'
    assert events[0].properties == props
    s.stage('page', props)
    assert s.deliver(current, 'page')['state'] == 'done'
    assert len(events) == 1


def test_結果不明Notion作成は再POSTしない(local):
    s = session()
    calls = []
    def lost(props):
        calls.append(props)
        raise RuntimeError('応答不明')
    with pytest.raises(RuntimeError):
        s.create(SimpleNamespace(create_page_once=lost), {'名前': '問い合わせ'})
    with pytest.raises(RuntimeError, match='作成結果が不明'):
        s.saved_page()
    assert len(calls) == 1
    assert PostgresCreationJournal().get('inquiry:test', 'inquiry_notion')['state'] == 'reserved'


def test_保存後の別編集を上書き配送しない(local):
    s = session(mapping=SimpleNamespace(zoho_id='zoho'), dispatch=lambda event: pytest.fail('古い値を送った'))
    s.stage('page', {'携帯番号': 'old'})
    assert s.deliver(client({'携帯番号': 'new'}), 'page')['state'] == 'blocked'
    with pytest.raises(ValueError):
        s.stage('page', {'部署': '対象外'})


def test_stage直後も問い合わせ由来が残り曖昧候補も永続保留(local):
    s = session()
    s.stage('page', {'携帯番号': '09000000000'})
    assert PostgresCreationJournal().get('notion:page', 'inquiry_handoff')['state'] == 'blocked'
    other = inquiry.InquirySession(s.flow, 'inquiry:ambiguous')
    assert other.ambiguous('同じメールの候補が複数')['state'] == 'blocked'
    assert other.read()['pageId'] is None
    assert PostgresCreationJournal().get(other.key, 'inquiry_handoff')['state'] == 'blocked'


def test_手動対応後の配送成功でこの入口のZoho保留だけを終端にする(local):
    s = session(dispatch=lambda event: SimpleNamespace(skipped=False, has_partial_skips=False))
    props = {'名前': '名前だけ'}
    s.stage('page', props)
    assert s.deliver(client(props), 'page')['state'] == 'blocked'
    s.flow.wiring.id_mapping_store.get = lambda page: SimpleNamespace(zoho_id='confirmed')
    assert s.deliver(client(props), 'page')['state'] == 'done'
    hold = PostgresCreationJournal().get('notion:page', 'zoho')
    assert hold['state'] == 'created' and hold['externalId'] == 'confirmed'


def test_別理由のZoho保留を配送成功だけで消さない(local):
    s = session(dispatch=lambda event: SimpleNamespace(skipped=False, has_partial_skips=False))
    props = {'名前': '名前だけ'}
    s.stage('page', props)
    s.deliver(client(props), 'page')
    PostgresCreationJournal().hold('notion:page', 'zoho', 'contact', '独立の確認事項')
    s.flow.wiring.id_mapping_store.get = lambda page: SimpleNamespace(zoho_id='confirmed')
    assert s.deliver(client(props), 'page')['state'] == 'done'
    assert PostgresCreationJournal().get('notion:page', 'zoho')['reason'] == '独立の確認事項'


def test_監視は保留の時刻と継続判定用IDを返す(local, monkeypatch):
    from src.diagnostics import registration_health
    monkeypatch.setattr(registration_health, 'connect', local)
    s = session()
    s.stage('page', {})
    first = registration_health.read_registration_health()
    assert first['status'] == 'pending' and first['count'] == 2
    assert first['activity_at'] and len(first['fingerprint']) == 64
    s._state('blocked', '確認事項')
    second = registration_health.read_registration_health()
    assert first['fingerprint'] == second['fingerprint']


@pytest.mark.parametrize('next_properties', [
    {'名前': '名前だけ'},
    {'携帯番号': '09000000000'},
])
def test_非空差分の再登録後も自身のZoho保留を配送成功で終端にする(local, next_properties):
    s = session(dispatch=lambda event: SimpleNamespace(skipped=False, has_partial_skips=False))
    props = {'名前': '名前だけ'}
    s.stage('page', props)
    s.deliver(client(props), 'page')
    s.stage('page', next_properties)
    assert s.read()['state'] == 'pending'
    assert s.read()['zohoHoldReason']
    s.flow.wiring.id_mapping_store.get = lambda page: SimpleNamespace(zoho_id='confirmed')
    # 途中で項目配送が保留され、表面上の理由が変わっても由来を保持する。
    s.flow.wiring.dispatcher.dispatch = lambda event: SimpleNamespace(skipped=True)
    current = client(dict(props, **next_properties))
    assert s.deliver(current, 'page')['state'] == 'blocked'
    s.flow.wiring.dispatcher.dispatch = lambda event: SimpleNamespace(skipped=False, has_partial_skips=False)
    assert s.deliver(current, 'page')['state'] == 'done'
    hold = PostgresCreationJournal().get('notion:page', 'zoho')
    assert hold['state'] == 'created' and hold['externalId'] == 'confirmed'
    assert s.read()['zohoHoldReason'] is None


def test_公開復旧関数を同じ差分で再実行しても保留を終端にする(local):
    from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
    store = SQLiteIdMappingStore()
    try:
        wiring = SimpleNamespace(id_mapping_store=store,
            dispatcher=SimpleNamespace(dispatch=lambda event: SimpleNamespace(skipped=False, has_partial_skips=False)))
        props = {'名前': '名前だけ'}
        contact = client(props)
        assert inquiry.enqueue_existing_inquiry('page', props, contact=contact, wiring=wiring)['state'] == 'blocked'
        store.upsert(IdMapping(notion_key='page', db_key='contact', zoho_id='confirmed'))
        assert inquiry.enqueue_existing_inquiry('page', props, contact=contact, wiring=wiring)['state'] == 'done'
        assert PostgresCreationJournal().get('notion:page', 'zoho')['state'] == 'created'
    finally:
        store.close()


def test_理由確認直後に別理由へ変わったZoho保留は終端にしない(local):
    s = session(dispatch=lambda event: SimpleNamespace(skipped=False, has_partial_skips=False))
    props = {'名前': '名前だけ'}
    s.stage('page', props)
    assert s.deliver(client(props), 'page')['state'] == 'blocked'
    s.flow.wiring.id_mapping_store.get = lambda page: SimpleNamespace(zoho_id='confirmed')
    original_get = s.flow.journal.get
    def concurrent_get(source, target):
        row = original_get(source, target)
        if target == 'zoho':
            # 理由を読んだ直後、終端UPDATE前に別の確認事項が登録される。
            PostgresCreationJournal().hold(source, target, 'contact', '他処理による新しい確認事項')
        return row
    s.flow.journal.get = concurrent_get
    assert s.deliver(client(props), 'page')['state'] == 'done'
    actual = PostgresCreationJournal().get('notion:page', 'zoho')
    assert actual['state'] == 'blocked' and actual['reason'] == '他処理による新しい確認事項'


@pytest.mark.parametrize('flag', ['archived', 'in_trash'])
def test_完了後に保存先が退避されても成功扱いせず保留へ戻す(local, flag):
    s = session(mapping=SimpleNamespace(zoho_id='confirmed'),
                dispatch=lambda event: SimpleNamespace(skipped=False, has_partial_skips=False))
    props = {'名前': '保存済み'}
    s.stage('page', props)
    assert s.deliver(client(props), 'page')['state'] == 'done'
    invalid = client(props)
    invalid.get_raw_page = lambda page: {flag: True}
    assert s.check_page(invalid, 'page')['state'] == 'blocked'
    assert s.read()['state'] == 'blocked'
    assert s.deliver(invalid, 'page')['state'] == 'blocked'


def test_完了後の統合旧ページも明示保留にする(local, monkeypatch):
    s = session()
    s.stage('page', {})
    s._state('done', '')
    monkeypatch.setattr('src.record_merge.aliases.resolve_alias', lambda *a: 'canonical')
    assert s.check_page(client({}), 'page')['state'] == 'blocked'
    assert '統合' in s.read()['reason']


def test_曖昧保留は入力を保存し同じキーで再投入できる(local):
    s = session()
    payload = dict(company='架空会社', name='問い合わせ', email='sample@example.com', phone='0000000000')
    s.remember_payload(payload)
    s.ambiguous('同定待ち')
    assert s.read()['inputPayload'] == payload
    s.stage('confirmed-page', {'名前': payload['name']})
    assert s.read()['inputPayload'] == payload and s.read()['pageId'] == 'confirmed-page'


def test_公開復旧は通常入口の保留を引き継いで1回で終端にする(local):
    from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
    store = SQLiteIdMappingStore()
    try:
        s = session()
        props = {'名前': '名前だけ'}
        s.stage('page', props)
        s.deliver(client(props), 'page')
        store.upsert(IdMapping(notion_key='page', db_key='contact', zoho_id='confirmed'))
        wiring = SimpleNamespace(id_mapping_store=store,
            dispatcher=SimpleNamespace(dispatch=lambda event: SimpleNamespace(skipped=False, has_partial_skips=False)))
        assert inquiry.enqueue_existing_inquiry('page', props, contact=client(props), wiring=wiring)['state'] == 'done'
        assert s.read()['state'] == 'done'
        assert PostgresCreationJournal().get('notion:page', 'zoho')['state'] == 'created'
        with local() as conn:
            assert conn.execute('SELECT count(*) n FROM "InquiryHandoff"').fetchone()['n'] == 1
    finally:
        store.close()


def test_確定404は保留応答で一時障害は例外と保留を残す(local):
    from src.sync_engine.clients.notion_client import NotionApiError
    s = session()
    s.stage('page', {})
    c = client({})
    c.get_raw_page = lambda page: (_ for _ in ()).throw(NotionApiError(404, 'not found'))
    assert s.check_page(c, 'page')['state'] == 'blocked'
    assert s.read()['state'] == 'blocked'
    c.get_raw_page = lambda page: (_ for _ in ()).throw(NotionApiError(503, 'unavailable'))
    with pytest.raises(NotionApiError):
        s.check_page(c, 'page')
    assert s.read()['state'] == 'blocked'


def test_公開復旧は異なる差分でも完了行を再配送しない(local):
    from src.sync_engine.id_mapping import SQLiteIdMappingStore
    store = SQLiteIdMappingStore()
    try:
        s = session()
        s.stage('page', {'名前': '完了時の値'})
        s._state('done', '')
        wiring = SimpleNamespace(id_mapping_store=store,
            dispatcher=SimpleNamespace(dispatch=lambda event: pytest.fail('完了行を再配送した')))
        result = inquiry.enqueue_existing_inquiry('page', {'名前': '新しい差分'}, contact=client({}), wiring=wiring)
        assert result['state'] == 'done'
        assert s.read()['state'] == 'done' and s.read()['properties'] == {'名前': '完了時の値'}
    finally:
        store.close()
