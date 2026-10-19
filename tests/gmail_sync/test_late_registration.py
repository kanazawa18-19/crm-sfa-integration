"""遅延登録の入口差・期間・失敗再開を隔離DBと純粋な偽APIで検証する。"""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4
import os
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from src.gmail_sync import late_registration as late

NOW=datetime(2026,10,14,tzinfo=timezone.utc)

@pytest.fixture
def local(monkeypatch):
    dsn=os.environ.get('SYNC_REVIEW_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('明示したローカルDBのみ')
    assert urlparse(dsn).hostname in {'localhost','127.0.0.1'}
    assert urlparse(dsn).path=='/crm_field_review_test'
    schema='late_'+uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    def connect():
        return psycopg.connect(dsn,row_factory=dict_row,options=f'-csearch_path={schema}')
    monkeypatch.setattr(late.db,'_connect',connect)
    from contextlib import nullcontext
    monkeypatch.setattr(late,'record_guard',lambda _:nullcontext())
    monkeypatch.setattr(late,'merge_block_reason',lambda _:None)
    with connect() as conn:
        conn.execute(Path('dashboard/prisma/migrations/20261014020000_gmail_late_registration/migration.sql').read_text())
    yield connect
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def page(email='a@example.com',id='page',edited=NOW):
    return dict(id=id,created_time=(NOW-timedelta(days=10)).isoformat(),last_edited_time=edited.isoformat(),
                properties={late.EMAIL_PROPERTY:dict(email=email)})


def job(store):
    store.observe(page(),['rep@example.com'],NOW,NOW,False)
    return store.jobs(now=NOW+timedelta(days=10))[0]


def dependencies(source=None):
    source=source or page()
    return dict(client=SimpleNamespace(get_raw_page=lambda _:source,
                query_raw=lambda _:dict(results=[source],has_more=False)),
                connection_reader=lambda _:SimpleNamespace(refresh_token_enc='fake'),decrypt=lambda v:v,
                refresh=lambda _:'fake',exists=lambda _:False,insert=lambda rows:len(rows),
                list_page=lambda *a,**k:SimpleNamespace(refs=[SimpleNamespace(id='m1')],next_page_token=None),
                fetch=lambda _,id:SimpleNamespace(label_ids=['INBOX'],id=id,from_header='a@example.com',to_header='rep@example.com',
                    internal_date_ms=str(int((NOW-timedelta(days=10)).timestamp()*1000)),
                    thread_id='t',subject='sample',snippet='sample'))


def test_無効時はDBも外部APIも触らない(monkeypatch):
    monkeypatch.delenv('GMAIL_LATE_REGISTRATION_ENABLED',raising=False)
    monkeypatch.setattr(late.db,'_connect',lambda:pytest.fail('DBへ接続した'))
    assert late.run()==dict(status='disabled')
    assert late.health()['status']=='ok'


def test_初期既存値はbaselineのみ変更と元メール復帰は別世代(local):
    store=late.Store()
    old=page(edited=NOW-timedelta(days=1))
    assert store.observe(old,['r'],NOW,NOW,True)==0
    assert store.jobs(now=NOW+timedelta(days=10))==[]
    assert store.observe(page('b@example.com'),['r'],NOW,NOW,False)==1
    assert store.observe(page('a@example.com'),['r'],NOW+timedelta(days=1),NOW,False)==1
    assert store.observe(page('a@example.com'),['r'],NOW+timedelta(days=2),NOW,False)==0
    rows=store.jobs(now=NOW+timedelta(days=10))
    assert len(rows)==2 and rows[0]['id']!=rows[1]['id']
    assert all(row['data']['before']-row['data']['after']==365*86400 for row in rows)


def test_初回巡回開始後の変更はジョブ化し途中失敗でも同じcursorから再開(local):
    store=late.Store(); calls=[]
    def response(body):
        calls.append(body)
        new_page=page();new_page['created_time']=NOW.isoformat()
        return dict(results=[new_page],has_more=True,next_cursor='next')
    real_observe=store.observe_batch
    store.observe_batch=lambda *a: (_ for _ in ()).throw(RuntimeError('中断'))
    with pytest.raises(RuntimeError):
        late.discover(store,SimpleNamespace(query_raw=response),['r'],NOW)
    assert store.state()['cursor'] is None
    store.observe_batch=real_observe
    late.discover(store,SimpleNamespace(query_raw=response),['r'],NOW+timedelta(hours=1))
    assert store.state()['cursor']=='next'
    assert len(store.jobs(now=NOW+timedelta(days=10)))==1
    assert len(calls)==1  # 固定済みページは再検索しない。


def test_日付境界の大量同時作成は完了としない(local):
    store=late.Store();store.state()
    store.save_state(dict(initial=True,cutoff=NOW.isoformat(),cursor='old',watermark=page()['created_time'],roundCount=9000))
    client=SimpleNamespace(query_raw=lambda _:dict(results=[page()],has_more=False,request_status=dict(type='incomplete')))
    with pytest.raises(ValueError,match='停滞'):
        late.discover(store,client,['r'],NOW)
    assert store.state()['initial'] is True


def test_2日待機は外部APIに触れない(local):
    store=late.Store();row=job(store)
    assert late.process_job(store,row,SimpleNamespace(),NOW)=='waiting'


def test_保存済IDはGmail取得もinsertも行わず終端しdoneは再実行しない(local):
    store=late.Store();row=job(store);deps=dependencies()
    deps.update(exists=lambda _:True,fetch=lambda *_:pytest.fail('完了メール再取得'),insert=lambda _:pytest.fail('再保存'))
    assert late.process_job(store,row,now=NOW+timedelta(days=2),**deps)=='done'
    with local() as conn:
        done=conn.execute('SELECT * FROM "GmailLateRegistrationJob"').fetchone()
    assert done['data']['existing']==1
    assert late.process_job(store,done,SimpleNamespace(),NOW+timedelta(days=3))=='waiting'


def test_ページ途中失敗は残IDだけを再開する(local):
    store=late.Store();row=job(store);deps=dependencies();saved=[]
    basefetch=deps['fetch']
    deps['list_page']=lambda *a,**k:SimpleNamespace(refs=[SimpleNamespace(id='m1'),SimpleNamespace(id='m2')],next_page_token='next')
    deps['fetch']=lambda token,id:basefetch(token,id) if id=='m1' else (_ for _ in ()).throw(RuntimeError('一時失敗'))
    deps['insert']=lambda rows:saved.extend(r.gmail_message_id for r in rows) or len(rows)
    with pytest.raises(RuntimeError):
        late.process_job(store,row,now=NOW+timedelta(days=2),**deps)
    retry=store.jobs(now=NOW+timedelta(days=10))[0]
    assert retry['data']['pending']==['m2'] and retry['data']['token'] is None
    deps['fetch']=basefetch
    deps['list_page']=lambda *a,**k:pytest.fail('確定済みページの再検索')
    assert late.process_job(store,retry,now=NOW+timedelta(days=2),**deps)=='pending'
    assert saved==['m1','m2'] and store.jobs(now=NOW+timedelta(days=10))[0]['data']['token']=='next'


def test_確定404はID証跡を残し未知の失敗は終端しない(local):
    store=late.Store();row=job(store);deps=dependencies()
    deps['fetch']=lambda *_:(_ for _ in ()).throw(late.gmail_client.GmailApiError(404,'missing'))
    assert late.process_job(store,row,now=NOW+timedelta(days=2),**deps)=='blocked'
    with local() as conn:
        assert conn.execute('SELECT data FROM "GmailLateRegistrationJob"').fetchone()['data']['missing']==['m1']


@pytest.mark.parametrize('change',['email','duplicate','archived'])
def test_原本変更や重複候補は保存せず保留(local,change):
    store=late.Store();row=job(store);source=page();deps=dependencies(source)
    if change=='email':source['properties'][late.EMAIL_PROPERTY]['email']='b@example.com'
    if change=='archived':source['archived']=True
    if change=='duplicate':deps['client'].query_raw=lambda _:dict(results=[source,source],has_more=False)
    deps['insert']=lambda _:pytest.fail('保留対象を保存した')
    assert late.process_job(store,row,now=NOW+timedelta(days=2),**deps)=='blocked'


def test_限定投入は狭い固定期間でdone再投入を無視(local,monkeypatch):
    monkeypatch.setenv('GMAIL_LATE_REGISTRATION_ENABLED','true')
    monkeypatch.setattr(late.db,'list_gmail_connections',lambda:[SimpleNamespace(rep_email='rep@example.com')])
    args=dict(page_ids=['page'],after=NOW-timedelta(days=11),before=NOW-timedelta(days=9),client=dependencies()['client'],now=NOW)
    assert late.enqueue_selected(**args)['jobs']==1
    assert late.Store().jobs(now=NOW+timedelta(days=10))==[]
    result=late.enqueue_selected(**args,apply=True)
    with local() as conn:
        conn.execute('UPDATE "GmailLateRegistrationJob" SET state=\'done\'')
    assert late.enqueue_selected(**args,apply=True)['ids']==result['ids']
    assert late.Store().jobs(now=NOW+timedelta(days=10))==[]
    with pytest.raises(ValueError):
        late.enqueue_selected(**{**args,'before':NOW},apply=True)


def test_監視は待機と失敗を分け本文宛先を出さない(local,monkeypatch):
    monkeypatch.setenv('GMAIL_LATE_REGISTRATION_MODE','active')
    monkeypatch.setenv('GMAIL_LATE_REGISTRATION_ENABLED','true')
    store=late.Store();store.state();store.save_state(dict(lastProgressAt=NOW.isoformat(),initial=False))
    job(store)
    result=late.health(NOW)
    assert result['status']=='ok' and 'example.com' not in str(result)
    store.save_state(dict(lastProgressAt=(NOW+timedelta(days=3)).isoformat(),initial=False))
    result=late.health(NOW+timedelta(days=3))
    assert result['status']=='pending' and result['activity_at'] and result['fingerprint']
    assert 'example.com' not in str(result)
    assert late.health(NOW+timedelta(days=4))['status']=='unknown'


def test_初見既存ページの他項目更新は自動過去取込せず候補保留(local):
    store=late.Store()
    store.observe(page(),['r'],NOW,NOW,True)
    assert store.jobs(now=NOW+timedelta(days=10))==[]
    with local() as conn:
        row=conn.execute('SELECT * FROM "GmailLateRegistrationJob"').fetchone()
    assert row['state']=='candidate' and '不明' in row['data']['reason']


def test_実保存は通知スコア効果なしで一意IDだけ記録し確定応答消失も重複しない(local):
    with local() as conn:
        conn.execute('''CREATE TABLE "EmailLog" (id TEXT PRIMARY KEY,"contactPageId" TEXT,"contactEmail" TEXT,
            "repEmail" TEXT,"gmailMessageId" TEXT UNIQUE,"gmailThreadId" TEXT,direction TEXT,subject TEXT,
            snippet TEXT,"sentAt" TIMESTAMPTZ,"createdAt" TIMESTAMPTZ,"incidentScore" INT,"incidentPriority" TEXT)''')
        conn.execute('CREATE TABLE "GmailIngestEffect" (id TEXT)')
    store=late.Store();row=job(store);deps=dependencies()
    deps['insert']=late.db.insert_email_logs
    deps['exists']=late.db.email_log_exists
    original_save=store.save_job
    def lost_ack(item,state='pending'):
        if item['data']['inserted']:
            raise RuntimeError('保存後の応答消失')
        original_save(item,state)
    store.save_job=lost_ack
    with pytest.raises(RuntimeError):
        late.process_job(store,row,now=NOW+timedelta(days=2),**deps)
    store.save_job=original_save
    retry=store.jobs(now=NOW+timedelta(days=10))[0]
    assert retry['data']['pending']==['m1']
    deps['fetch']=lambda *_:pytest.fail('保存済みメールを再取得')
    assert late.process_job(store,retry,now=NOW+timedelta(days=2),**deps)=='done'
    with local() as conn:
        logs=conn.execute('SELECT * FROM "EmailLog"').fetchall()
        assert len(logs)==1 and logs[0]['incidentScore'] is None and logs[0]['incidentPriority'] is None
        assert conn.execute('SELECT count(*) n FROM "GmailIngestEffect"').fetchone()['n']==0


def test_メール取得中に連絡先変更ならinsertしない(local):
    store=late.Store();row=job(store);source=page();deps=dependencies(source);fetch=deps['fetch']
    def changed(token,id):
        source['properties'][late.EMAIL_PROPERTY]['email']='changed@example.com'
        return fetch(token,id)
    deps['fetch']=changed
    deps['insert']=lambda _:pytest.fail('変更後の連絡先に保存')
    assert late.process_job(store,row,now=NOW+timedelta(days=2),**deps)=='blocked'


def test_失効したGmailページtokenは同じ固定期間の先頭へ戻して再開(local):
    store=late.Store();row=job(store);row['data']['token']='expired';store.save_job(row)
    original=(row['data']['after'],row['data']['before']);deps=dependencies()
    deps['list_page']=lambda *a,**k:(_ for _ in ()).throw(late.gmail_client.GmailApiError(400,'invalid page'))
    with pytest.raises(late.gmail_client.GmailApiError):
        late.process_job(store,row,now=NOW+timedelta(days=2),**deps)
    retry=store.jobs(now=NOW+timedelta(days=10))[0]
    assert retry['data']['token'] is None and (retry['data']['after'],retry['data']['before'])==original


def test_失効したNotioncursorでも初回cutoffを保持(local):
    from src.sync_engine.clients.notion_client import NotionApiError
    store=late.Store();store.state();store.save_state(dict(initial=True,cutoff=NOW.isoformat(),cursor='old',watermark=None,roundCount=100))
    client=SimpleNamespace(query_raw=lambda _:(_ for _ in ()).throw(NotionApiError(400,'invalid cursor')))
    with pytest.raises(NotionApiError):
        late.discover(store,client,['r'],NOW+timedelta(hours=1))
    assert store.state()['cutoff']==NOW.isoformat() and store.state()['cursor'] is None


def test_検索範囲は固定秒境界でアドレスを引用する():
    value=late.query_for(dict(email='a@example.com',after=100,before=200))
    assert 'after:99 before:200' in value and 'from:"a@example.com"' in value
    with pytest.raises(ValueError):
        late.query_for(dict(email='a@example.com OR anything',after=100,before=200))


def test_連絡先404は保留で残し外部取得に進まない(local):
    from src.sync_engine.clients.notion_client import NotionApiError
    store=late.Store();row=job(store);deps=dependencies()
    deps['client'].get_raw_page=lambda _:(_ for _ in ()).throw(NotionApiError(404,'missing'))
    deps['list_page']=lambda *a,**k:pytest.fail('原本不明で検索した')
    assert late.process_job(store,row,now=NOW+timedelta(days=2),**deps)=='blocked'


def test_100待機ジョブより後ろの期限済みジョブもLIMIT前に選ぶ(local):
    from psycopg.types.json import Jsonb
    store=late.Store();row=job(store)
    with local() as conn:
        for n in range(100):
            data={**row['data'],'readyAt':(NOW+timedelta(days=20)).isoformat()}
            conn.execute('INSERT INTO "GmailLateRegistrationJob" (id,"pageId",data,"updatedAt") VALUES (%s,%s,%s,%s)',
                (f'waiting{n}','page',Jsonb(data),NOW))
    assert [item['id'] for item in store.jobs(now=NOW+timedelta(days=3))]==[row['id']]


def test_限定ジョブIDは同じページの365日自動ジョブを処理対象に含めない(local,monkeypatch):
    store=late.Store();automatic=job(store)
    monkeypatch.setenv('GMAIL_LATE_REGISTRATION_ENABLED','true')
    monkeypatch.setattr(late.db,'list_gmail_connections',lambda:[SimpleNamespace(rep_email='rep@example.com')])
    selected=late.enqueue_selected(['page'],after=NOW-timedelta(days=11),before=NOW-timedelta(days=9),
        client=dependencies()['client'],now=NOW,apply=True)
    rows=store.jobs(selected['ids'],now=NOW+timedelta(days=3))
    assert [item['id'] for item in rows]==selected['ids']
    assert rows[0]['data']['before']-rows[0]['data']['after']==2*86400
    assert store.jobs([automatic['id']],now=NOW+timedelta(days=3))==[]


def test_時間切れでも100件ページの20件を原子的に確定し次回残80件から進む(local):
    store=late.Store();pages=[page(id=f'p{i}',edited=NOW-timedelta(days=1)) for i in range(100)]
    calls=[]
    client=SimpleNamespace(query_raw=lambda body:calls.append(body) or dict(results=pages,has_more=True,next_cursor='next'))
    times=iter([0,0,200])
    assert late.discover(store,client,['r'],NOW,deadline=180,clock=lambda:next(times))==20
    state=store.state()
    assert state['cursor'] is None and len(state['pendingPages'])==80
    with local() as conn:
        assert conn.execute('SELECT count(*) n FROM "GmailContactEmail"').fetchone()['n']==20
    assert late.discover(store,client,['r'],NOW+timedelta(minutes=5),deadline=180,clock=lambda:0)==80
    assert len(calls)==1 and store.state()['cursor']=='next' and 'pendingPages' not in store.state()
    with local() as conn:
        assert conn.execute('SELECT count(*) n FROM "GmailContactEmail"').fetchone()['n']==100


def test_バッチ内失敗は既知値ジョブとページ内進捗を全部ロールバック(local):
    store=late.Store();store.state()
    with local() as conn:
        conn.execute('''ALTER TABLE "GmailContactEmail" ADD CONSTRAINT reject_page CHECK ("pageId"<>'bad')''')
    client=SimpleNamespace(query_raw=lambda _:dict(results=[page(id='good'),page(id='bad')],has_more=False))
    with pytest.raises(psycopg.errors.CheckViolation):
        late.discover(store,client,['r'],NOW)
    assert len(store.state()['pendingPages'])==2 and store.state()['initial'] is True
    with local() as conn:
        assert conn.execute('SELECT count(*) n FROM "GmailContactEmail"').fetchone()['n']==0
        assert conn.execute('SELECT count(*) n FROM "GmailLateRegistrationJob"').fetchone()['n']==0


def test_1バッチ20件でもDB接続は1つだけ(local,monkeypatch):
    store=late.Store();count=[]
    def counted():
        count.append(1)
        return local()
    monkeypatch.setattr(late.db,'_connect',counted)
    store.observe_batch([page(id=f'p{i}') for i in range(20)],['r'],NOW,NOW,False)
    assert len(count)==1


@pytest.mark.parametrize('when',['before','during_fetch','inside_guard'])
def test_統合旧ページや統合進行を保存前に止め移管先へ付け替えない(local,monkeypatch,when):
    from contextlib import contextmanager
    store=late.Store();row=job(store);deps=dependencies();blocked=[when=='before'];fetch=deps['fetch']
    monkeypatch.setattr(late,'merge_block_reason',lambda _:'統合済みまたは統合中' if blocked[0] else None)
    def fetch_and_change(token,id):
        if when=='during_fetch':blocked[0]=True
        return fetch(token,id)
    deps['fetch']=fetch_and_change
    @contextmanager
    def guard(_):
        if when=='inside_guard':blocked[0]=True
        yield
    monkeypatch.setattr(late,'record_guard',guard)
    deps['insert']=lambda _:pytest.fail('統合旧ページへ保存した')
    assert late.process_job(store,row,now=NOW+timedelta(days=2),**deps)=='blocked'
    with local() as conn:
        actual=conn.execute('SELECT data FROM "GmailLateRegistrationJob"').fetchone()['data']
    assert actual['pageId']=='page' and '統合' in actual['reason']


def test_実aliasと統合中判定を使用しDB失敗は正常扱いにしない(monkeypatch):
    from src.record_merge import aliases
    from src.sync_engine.record_sync_lock import RecordSyncBusy
    monkeypatch.setattr(aliases,'resolve_alias',lambda *a:'target')
    assert '旧連絡先' in late.merge_block_reason('old')
    monkeypatch.setattr(aliases,'resolve_alias',lambda *a:None)
    monkeypatch.setattr(aliases,'require_record_available',lambda *a:(_ for _ in ()).throw(RecordSyncBusy()))
    assert '統合' in late.merge_block_reason('old')
    monkeypatch.setattr(aliases,'require_record_available',lambda *a:(_ for _ in ()).throw(RuntimeError('DB停止')))
    with pytest.raises(RuntimeError):late.merge_block_reason('old')


def test_下書きチャットを履歴検索から除く():
    assert '-in:drafts -in:chats' in late.query_for(dict(email='a@example.com',after=10,before=20))


def test_9000件境界はhas_moreでも検索境界を進める(local):
    store=late.Store();store.state()
    store.save_state(dict(initial=True,cutoff=NOW.isoformat(),cursor='old',watermark=None,roundCount=8999))
    client=SimpleNamespace(query_raw=lambda _:dict(results=[page()],has_more=True,next_cursor='next'))
    late.discover(store,client,['r'],NOW)
    assert store.state()['cursor'] is None
    assert store.state()['watermark']==page()['created_time']
    assert store.state()['initial'] is True


def test_同じ分に作成された新規はbaselineへ吸収しない(local):
    store=late.Store();source=page();source['created_time']=NOW.isoformat()
    late.discover(store,SimpleNamespace(query_raw=lambda _:dict(results=[source],has_more=False)),['r'],NOW+timedelta(seconds=40))
    assert store.state()['cutoff']==NOW.isoformat()
    assert len(store.jobs(now=NOW+timedelta(days=3)))==1


@pytest.fixture
def decision_locks(monkeypatch):
    monkeypatch.setenv('GMAIL_LATE_REGISTRATION_ENABLED','true')
    monkeypatch.setattr(late.db,'try_acquire_push_sync_lock',lambda _:object())
    monkeypatch.setattr(late.db,'release_push_sync_lock',lambda *_:None)


def test_候補判断は現物指紋一致が必須で監査と元待機期間を保持(local,decision_locks):
    store=late.Store();store.observe(page(),['r'],NOW,NOW,True)
    with local() as conn:row=conn.execute('SELECT * FROM "GmailLateRegistrationJob"').fetchone()
    client=dependencies()['client']
    preview=late.review_held_job(row['id'],client=client)
    assert preview['retry_eligible'] is True
    assert preview['state']==preview['prior_state']=='candidate'
    changed=dependencies(page('new@example.com'))['client']
    with pytest.raises(ValueError,match='変化'):
        late.review_held_job(row['id'],client=changed,action='retry',actor='operator',reason='メール変更確認',expected_fingerprint=preview['fingerprint'])
    applied=late.review_held_job(row['id'],client=client,action='retry',actor='operator',reason='メール変更確認',expected_fingerprint=preview['fingerprint'])
    assert applied['state']=='pending' and applied['prior_state']=='candidate'
    assert applied['fingerprint']==preview['fingerprint']
    queued=store.jobs(now=NOW+timedelta(days=3))[0]
    assert queued['data']['readyAt']==row['data']['readyAt']
    assert queued['data']['after']==row['data']['after']
    assert queued['data']['decisions'][0]['priorState']=='candidate'
    with pytest.raises(ValueError,match='候補または保留'):
        late.review_held_job(row['id'],client=client,action='retry',actor='operator',reason='再送',expected_fingerprint=preview['fingerprint'])


@pytest.mark.parametrize('terminal',['done','dismissed'])
def test_完了と取下げ済みは判断でも古いworkerでも再開不可(local,decision_locks,terminal):
    store=late.Store();row=job(store)
    with local() as conn:conn.execute('UPDATE "GmailLateRegistrationJob" SET state=%s',(terminal,))
    with pytest.raises(ValueError,match='候補または保留'):
        late.review_held_job(row['id'],client=dependencies()['client'])
    store.save_job(row)
    with local() as conn:assert conn.execute('SELECT state FROM "GmailLateRegistrationJob"').fetchone()['state']==terminal


def test_消失保留再試行は消失IDだけ再確認し取下げも監査する(local,decision_locks):
    store=late.Store();row=job(store)
    row['data']['missing']=['missing'];store.save_job(row,'blocked')
    args=dict(job_id=row['id'],client=dependencies()['client'])
    preview=late.review_held_job(**args)
    late.review_held_job(**args,action='retry',actor='operator',reason='原本復元を確認',expected_fingerprint=preview['fingerprint'])
    retried=store.jobs(now=NOW+timedelta(days=3))[0]
    assert retried['data']['pending']==['missing'] and retried['data']['missing']==[]
    assert retried['data']['decisions'][0]['missing']==['missing']
    store.save_job(retried,'blocked')
    preview=late.review_held_job(**args)
    applied=late.review_held_job(**args,action='dismiss',actor='operator',reason='消失のため取下げ判断',expected_fingerprint=preview['fingerprint'])
    assert applied['state']=='dismissed' and applied['prior_state']=='blocked'
    assert applied['fingerprint']==preview['fingerprint']
    with local() as conn:
        current=conn.execute('SELECT * FROM "GmailLateRegistrationJob"').fetchone()
    assert current['state']=='dismissed' and len(current['data']['decisions'])==2


def test_候補とbaseline待機は警告せず既存保留指紋は追加で変えない(local,monkeypatch):
    monkeypatch.setenv('GMAIL_LATE_REGISTRATION_ENABLED','true')
    store=late.Store();store.state();store.save_state(dict(lastProgressAt=NOW.isoformat(),initial=False))
    store.observe(page(id='candidate'),['r'],NOW-timedelta(days=5),NOW,True)
    row=job(store)
    result=late.health(NOW)
    assert result['status']=='ok' and result['candidate_count']==1
    store.save_job(row,'blocked')
    old=late.health(NOW)
    store.observe(page(id='other'),['r'],NOW-timedelta(days=5),NOW,False)
    later=store.jobs(now=NOW)[0];store.save_job(later,'blocked')
    assert late.health(NOW)['fingerprint']==old['fingerprint']


def test_複数ジョブは個別失敗と共有鍵競合でも次へ進み巡回障害にしない(local,monkeypatch,decision_locks):
    from src.sync_engine.record_sync_lock import RecordSyncBusy
    from src.sync_engine.clients.notion_client import HttpNotionClient
    monkeypatch.setenv('GMAIL_LATE_REGISTRATION_MODE','active')
    store=late.Store();store.state();store.save_state(dict(initial=False,lastProgressAt=NOW.isoformat()))
    for i in range(3):store.observe(page(id=f'p{i}'),['r'],NOW-timedelta(days=3),NOW,False)
    monkeypatch.setattr(late,'discover',lambda *a,**k:0)
    monkeypatch.setattr(HttpNotionClient,'__init__',lambda *a,**k:None)
    monkeypatch.setattr(late.db,'list_gmail_connections',lambda:[])
    calls=[]
    def process(store,row,*a,**kw):
        calls.append(row['pageId'])
        if len(calls)==1:raise RuntimeError('一時障害')
        if len(calls)==2:raise RecordSyncBusy('競合')
        store.save_job(row,'done')
    monkeypatch.setattr(late,'process_job',process)
    result=late.run()
    assert result['processed']==3 and result['failed']==1 and result['busy']==1
    assert 'error' not in store.state()


def test_ページ途中保留の消失再試行は残IDと次ページを失わない(local,decision_locks):
    store=late.Store();row=job(store)
    row['data'].update(missing=['gone'],pending=['unread'],token='previous',nextToken='next')
    store.save_job(row,'blocked')
    args=dict(job_id=row['id'],client=dependencies()['client'])
    preview=late.review_held_job(**args)
    late.review_held_job(**args,action='retry',actor='operator',reason='統合保留を確認',expected_fingerprint=preview['fingerprint'])
    data=store.jobs(now=NOW+timedelta(days=3))[0]['data']
    assert data['pending']==['gone','unread'] and data['nextToken']=='next'
