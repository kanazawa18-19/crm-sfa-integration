"""統合待ちの耐久保全・公平な再開・履歴前進を隔離DBで検証する。"""
from contextlib import nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
import os

import pytest
from src.gmail_sync import deferred, db, sync, gmail_client
from tests.record_merge.test_postgres import local
from tests.record_merge.test_email_history import history


def message(identifier='queued'):
    return gmail_client.GmailMessage(label_ids=['SENT'], id=identifier, from_header='buyer@customer.test',
        to_header='rep@internal.test', subject='合成', date_header=None, snippet='要約',
        body_text='保存する本文', raw_message='c3ludGhldGlj', internal_date_ms='1780000000000')


@pytest.fixture
def queue(history, monkeypatch):
    import psycopg
    from psycopg.rows import dict_row
    monkeypatch.setattr(db, '_connect', lambda: psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row))
    monkeypatch.setenv('DATABASE_URL_UNPOOLED', os.environ['DATABASE_URL'])
    with db._connect() as conn:
        for name in ('20261019000000_gmail_deferred_message', '20261001010000_gmail_ingest_effect'):
            conn.execute((Path('dashboard/prisma/migrations') / name / 'migration.sql').read_text())
    return lambda: db._connect()


def put(identifier='queued', contact='source'):
    deferred.enqueue(message(identifier), 'rep@internal.test', contact, 'buyer@customer.test', {'internal.test'})


def test_本文と初回日時は再投入で変わらず保留期限は試行で延長しない(queue):
    put()
    with queue() as conn:
        conn.execute('''UPDATE "GmailDeferredMessage" SET "createdAt"=now()-interval '40 minutes' ''')
        before = conn.execute('SELECT * FROM "GmailDeferredMessage"').fetchone()
    deferred.enqueue(replace(message(), body_text='別の本文'), 'rep@internal.test', 'other', 'buyer@customer.test', set())
    deferred._finish(before, deferred.DeferredContactBusy('source', 'buyer@customer.test'))
    with queue() as conn:
        after = conn.execute('SELECT * FROM "GmailDeferredMessage"').fetchone()
    assert after['message'] == asdict(message())
    assert after['contactPageId'] == 'source' and after['createdAt'] == before['createdAt']
    assert after['attempts'] == 1 and after['nextAttemptAt'] > before['nextAttemptAt']
    assert deferred.health()['deferred_overdue_count'] == 1


def test_同じ連絡先の大量保留より別連絡先を先に回す(queue):
    for identifier in ('a1', 'a2', 'a3'):
        put(identifier)
    put('b1', 'other')
    assert {row['gmailMessageId'] for row in deferred._due(10)} == {'a1', 'b1'}


def test_統合待ちを残して別連絡先を完了し本文を消す(queue, monkeypatch):
    put('a1'); put('b1', 'other')
    def process(row, _client):
        if row['contactPageId'] == 'source':
            raise deferred.DeferredContactBusy('source', 'buyer@customer.test')
    monkeypatch.setattr(deferred, '_process', process)
    result = deferred.drain(client=object())
    assert result == dict(status='ok', done=1, waiting=1, failed=0)
    with queue() as conn:
        rows = conn.execute('SELECT * FROM "GmailDeferredMessage" ORDER BY "gmailMessageId"').fetchall()
    assert rows[0]['state'] == 'pending' and rows[0]['message']['body_text']
    assert rows[1]['state'] == 'done' and rows[1]['message'] == {}
    put('b1', 'other')
    assert not deferred._due(10)


def test_再開完了直後の停止でも通知とログを二重作成しない(queue, monkeypatch):
    put()
    row = deferred._due(1)[0]
    monkeypatch.setattr(sync, 'contact_record_guard', lambda *_: nullcontext('target'))
    monkeypatch.setattr(sync, 'score_email', lambda *_: (0, None))
    delivered = []
    monkeypatch.setattr(sync, 'notify_web_engagement_tool', lambda **_: delivered.append(True))
    from src.email_review import service
    monkeypatch.setattr(service, 'safe_capture', lambda *_: True)
    client = SimpleNamespace(update_page=lambda *_: None)
    deferred._process(row, client)
    # 完了記録の直前でプロセスが落ちた状態から同じ本文を再処理。
    deferred._process(row, client)
    assert delivered == [True]
    with queue() as conn:
        logs = conn.execute('SELECT * FROM "EmailLog" WHERE "gmailMessageId"=\'queued\'').fetchall()
    assert len(logs) == 1 and logs[0]['contactPageId'] == 'target'
    assert all(db.gmail_ingest_effect_status('queued').values())


@pytest.mark.parametrize('persist_fails', [False, True])
def test_保全成功のみ履歴を進め次のメールへ進む(monkeypatch, persist_fails):
    from tests.gmail_sync.test_sync import _incremental_fixture
    fetched, saved = _incremental_fixture(monkeypatch, records=[('2000', ['first', 'second'])])
    seen = []
    def record(msg, *_args, **_kwargs):
        seen.append(msg.id)
        if msg.id == 'first':
            raise deferred.DeferredContactBusy('source', 'buyer@customer.test')
        return True
    monkeypatch.setattr(sync, '_record_message', record)
    from src.email_review import service
    monkeypatch.setattr(service, 'safe_capture', lambda *_: True)
    queued = []
    def enqueue(*args):
        if persist_fails:
            raise RuntimeError('合成DB停止')
        queued.append(args[0].id)
    monkeypatch.setattr(deferred, 'enqueue', enqueue)
    if persist_fails:
        with pytest.raises(RuntimeError):
            sync.sync_rep_incremental('rep@internal.test', 'fake', object(), internal_domains=frozenset())
        assert saved == [] and seen == ['first']
    else:
        assert sync.sync_rep_incremental('rep@internal.test', 'fake', object(), internal_domains=frozenset()) == 1
        assert seen == ['first', 'second'] and queued == ['first']
        assert saved[-1][1] == '9000'


def test_GASの耐久保留を取込み完了扱いにしない(monkeypatch):
    from src.sync_engine.webhook_handlers import gmail_ingest_webhook as handler
    monkeypatch.setattr(db, 'email_log_exists', lambda _: False)
    monkeypatch.setattr(sync, 'record_message', lambda *_a, **_k: deferred.DEFERRED)
    assert handler._process_logged_message(message(), 'rep@internal.test', object(), lambda _: 'source', False) == 'effect_failed'


def test_本文取得404は削除済みスキップへ変換されない(monkeypatch):
    from src.gmail_sync import token_crypto
    monkeypatch.setattr(db, 'find_connection_by_email', lambda _: SimpleNamespace(refresh_token_enc='fake'))
    monkeypatch.setattr(token_crypto, 'decrypt_token', lambda _: 'fake')
    monkeypatch.setattr(gmail_client, 'refresh_access_token', lambda _: 'fake')
    def missing(*_args, **_kwargs):
        raise gmail_client.GmailApiError(404, 'synthetic')
    monkeypatch.setattr(gmail_client, 'get_raw_message', missing)
    with pytest.raises(RuntimeError, match='本文'):
        deferred.enqueue(replace(message(), raw_message=None), 'rep@internal.test', 'source', 'buyer@customer.test', set())


def test_抽出不可の長文も原文を保全してから受理する(queue, monkeypatch):
    from src.gmail_sync import token_crypto
    raw = 'eA' * 20001
    monkeypatch.setattr(db, 'find_connection_by_email', lambda _: SimpleNamespace(refresh_token_enc='fake'))
    monkeypatch.setattr(token_crypto, 'decrypt_token', lambda _: 'fake')
    monkeypatch.setattr(gmail_client, 'refresh_access_token', lambda _: 'fake')
    monkeypatch.setattr(gmail_client, 'get_raw_message', lambda *_: raw)
    deferred.enqueue(replace(message(), body_text='', raw_message=None), 'rep@internal.test', 'source', 'buyer@customer.test', set())
    with queue() as conn:
        saved = conn.execute('SELECT message FROM "GmailDeferredMessage"').fetchone()['message']
    assert saved['body_text'] == '' and saved['raw_message'] == raw


def test_残り時間が無いと保留を消さず次回へ回す(queue, monkeypatch):
    put()
    monkeypatch.setattr(deferred, '_process', lambda *_: pytest.fail('時間切れで処理開始した'))
    assert deferred.drain(seconds=0, client=object())['done'] == 0
    assert len(deferred._due(1)) == 1


def test_本文抽出不可は完了後も原文を監査用に残し再配送しない(queue):
    deferred.enqueue(replace(message(), body_text=''), 'rep@internal.test', 'source', 'buyer@customer.test', set())
    row = deferred._due(1)[0]
    deferred._finish(row)
    with queue() as conn:
        saved = conn.execute('SELECT state,message FROM "GmailDeferredMessage"').fetchone()
    assert saved['state'] == 'done' and saved['message']['raw_message'] == message().raw_message
    assert deferred._due(1) == []


def test_旧保留はmetadataで下書きを確定し原文を残して終了(queue, monkeypatch):
    deferred.enqueue(replace(message(), label_ids=None),'rep@internal.test','source','buyer@customer.test',set())
    monkeypatch.setattr(db,'find_connection_by_email',lambda _:SimpleNamespace(refresh_token_enc='fake'))
    monkeypatch.setattr('src.gmail_sync.token_crypto.decrypt_token',lambda _:'fake')
    monkeypatch.setattr(gmail_client,'refresh_access_token',lambda _:'fake')
    monkeypatch.setattr(gmail_client,'get_message_labels',lambda *_:['DRAFT'])
    monkeypatch.setattr(sync,'record_message',lambda *a,**k:pytest.fail('下書きを保存した'))
    assert deferred.drain(client=object())['done']==1
    with queue() as conn:
        row=conn.execute('SELECT * FROM "GmailDeferredMessage"').fetchone()
    assert row['state']=='done' and row['lastErrorType']=='ExcludedUnsent'
    assert row['message']['raw_message']==message().raw_message
    assert row['message']['body_text']==message().body_text
    assert row['message']['label_ids']==['DRAFT']
    assert deferred.drain(client=object())['done']==0


def test_旧保留のmetadata不明は原文を保持して再試行(queue, monkeypatch):
    deferred.enqueue(replace(message(), label_ids=None),'rep@internal.test','source','buyer@customer.test',set())
    monkeypatch.setattr(db,'find_connection_by_email',lambda _:None)
    assert deferred.drain(client=object())['failed']==1
    with queue() as conn:
        row=conn.execute('SELECT * FROM "GmailDeferredMessage"').fetchone()
    assert row['state']=='pending' and row['message']['raw_message']==message().raw_message


def test_Gmail実応答から共通保存と実DB受付まで送信証拠を保持(queue, monkeypatch, requests_mock):
    import base64
    from src.email_review import repository
    monkeypatch.setattr(repository,'_connect',db._connect)
    with queue() as conn:
        conn.execute('CREATE TABLE "ClientNameIndex" ("notionPageId" text PRIMARY KEY,"rawName" text,"normalizedName" text)')
        conn.execute(Path('dashboard/prisma/migrations/20261016090000_email_review/migration.sql').read_text())
    monkeypatch.setenv('EMAIL_REVIEW_ENABLED','true')
    monkeypatch.setenv('EMAIL_REVIEW_START_AT','2026-01-01T00:00:00+00:00')
    payload={'id':'sent-e2e','threadId':'thread','internalDate':'1780000000000','labelIds':['SENT'],
             'payload':{'mimeType':'text/plain','body':{'data':base64.urlsafe_b64encode('合成本文'.encode()).decode()},
                        'headers':[{'name':'From','value':'rep@internal.test'},
                                   {'name':'To','value':'buyer@customer.test'},
                                   {'name':'Message-ID','value':'<sent-e2e@example.test>'},
                                   {'name':'Subject','value':'合成送信'}]}}
    requests_mock.get('https://gmail.googleapis.com/gmail/v1/users/me/messages/sent-e2e',json=payload)
    mail=gmail_client.get_message('fake','sent-e2e')
    assert requests_mock.last_request.qs['format']==['full']
    assert mail.label_ids==['SENT']
    monkeypatch.setattr(sync,'contact_record_guard',lambda *_:nullcontext('target'))
    monkeypatch.setattr(sync,'score_email',lambda *_:(0,None))
    monkeypatch.setattr(sync,'notify_web_engagement_tool',lambda **_:None)
    assert sync.record_message(mail,'rep@internal.test',SimpleNamespace(update_page=lambda *_:None),
        internal_domains=frozenset({'internal.test'}),atomic_insert=True,resolve_contact=lambda _:'target')
    with queue() as conn:
        review=conn.execute('SELECT data FROM "EmailReview"').fetchone()['data']
        log=conn.execute('SELECT * FROM "EmailLog" WHERE "gmailMessageId"=%s',('sent-e2e',)).fetchone()
    assert review['message']['label_ids']==['SENT'] and review['state']=='new'
    assert review['message']['body_text']=='合成本文' and log['direction']=='outbound'
    # metadata取得もヘッダー絞込だけでrootのlabelIdsを落とさない。
    monkeypatch.setenv('EMAIL_REVIEW_ENABLED','false')
    assert gmail_client.get_message('fake','sent-e2e').label_ids==['SENT']
    assert requests_mock.last_request.qs['format']==['metadata']
    assert 'fields' not in requests_mock.last_request.qs
