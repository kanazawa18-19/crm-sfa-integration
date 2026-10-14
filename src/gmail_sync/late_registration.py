"""連絡先メールの遅延登録を、通常同期と分離した有界ジョブで回収する。"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from psycopg.types.json import Jsonb
from src.gmail_sync import db, gmail_client, sync
from src.gmail_sync.token_crypto import decrypt_token
from src.db_utils import ensure_utc

EMAIL_PROPERTY = 'メールアドレス'
STATE_KEY = 'contact-email-discovery'
PAGE_SIZE = 100
MESSAGE_LIMIT = 10


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return ensure_utc(value)


def email_of(page):
    value = (page.get('properties', {}).get(EMAIL_PROPERTY) or {}).get('email') or ''
    value = value.strip().lower()
    # 検索構文へ注入できる文字は受け付けない。
    return value if re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+\.[a-z]{2,}", value) else ''


def source_active(page):
    return not page.get('archived') and not page.get('in_trash')


def query_for(job):
    email = job['email']
    if email_of({'properties': {EMAIL_PROPERTY: {'email': email}}}) != email:
        raise ValueError('メール検索条件が不正です')
    return f'{{from:"{email}" to:"{email}"}} after:{job["after"]-1} before:{job["before"]} -in:spam -in:trash -in:drafts -in:chats'


class Store:
    def state(self):
        with db._connect() as conn:
            conn.execute('''INSERT INTO "GmailContactDiscovery" (key,data) VALUES (%s,'{}')
                ON CONFLICT DO NOTHING''', (STATE_KEY,))
            return conn.execute('SELECT data FROM "GmailContactDiscovery" WHERE key=%s', (STATE_KEY,)).fetchone()['data']

    def save_state(self, data):
        with db._connect() as conn:
            conn.execute('UPDATE "GmailContactDiscovery" SET data=%s,"updatedAt"=NOW() WHERE key=%s', (Jsonb(data),STATE_KEY))

    def observe(self, page, reps, now, cutoff, initial):
        return self.observe_batch([page], reps, now, cutoff, initial)

    def observe_batch(self, pages, reps, now, cutoff, initial, next_state=None):
        """1接続・一括SQLで既知値/ジョブ/ページ内進捗を原子的に確定する。"""
        with db._connect() as conn:
            conn.execute("SET LOCAL statement_timeout='10000ms'")
            known = {row['pageId']: row for row in conn.execute(
                'SELECT * FROM "GmailContactEmail" WHERE "pageId"=ANY(%s) FOR UPDATE',
                ([page['id'] for page in pages],)).fetchall()}
            snapshots, jobs = [], []
            enqueued = 0
            for page in pages:
                page_id, email = page['id'], email_of(page) if source_active(page) else ''
                previous = known.get(page_id)
                changed = previous is not None and previous['email'] != email
                recent = utc(page['last_edited_time']) >= cutoff
                baseline_unknown = previous is None and initial and recent and utc(page['created_time']) < cutoff
                enqueue = bool(email) and (changed or (previous is None and (not initial or recent)))
                generation = str(uuid4()) if previous is None or changed else previous['generation']
                if enqueue:
                    enqueued += 1
                    for rep in reps:
                        payload = dict(pageId=page_id,email=email,rep=rep,generation=generation,
                            after=int((now-timedelta(days=365)).timestamp()),before=int(now.timestamp()),
                            readyAt=(now+timedelta(days=2)).isoformat(),token=None,pending=None,
                            fetched=0,inserted=0,existing=0,missing=[],
                            reason='初回巡回中の既存メール変更有無が不明です' if baseline_unknown else '')
                        key = hashlib.sha256(f'{page_id}:{generation}:{rep}'.encode()).hexdigest()
                        jobs.append(dict(id=key,pageId=page_id,state='candidate' if baseline_unknown else 'pending',data=payload))
                snapshots.append(dict(pageId=page_id,email=email,generation=generation,observedAt=now.isoformat()))
            if snapshots:
                conn.execute('''INSERT INTO "GmailContactEmail" ("pageId",email,generation,"observedAt")
                    SELECT "pageId",email,generation,"observedAt" FROM jsonb_to_recordset(%s)
                    AS x("pageId" TEXT,email TEXT,generation TEXT,"observedAt" TIMESTAMPTZ)
                    ON CONFLICT ("pageId") DO UPDATE SET email=EXCLUDED.email,generation=EXCLUDED.generation,
                    "observedAt"=EXCLUDED."observedAt"''', (Jsonb(snapshots),))
            if jobs:
                conn.execute('''INSERT INTO "GmailLateRegistrationJob" (id,"pageId",state,data)
                    SELECT id,"pageId",state,data FROM jsonb_to_recordset(%s)
                    AS x(id TEXT,"pageId" TEXT,state TEXT,data JSONB) ON CONFLICT DO NOTHING''', (Jsonb(jobs),))
            if next_state is not None:
                conn.execute('UPDATE "GmailContactDiscovery" SET data=%s,"updatedAt"=NOW() WHERE key=%s',
                    (Jsonb(next_state),STATE_KEY))
            return enqueued

    def jobs(self, job_ids=None, *, now=None):
        with db._connect() as conn:
            return conn.execute('''SELECT * FROM "GmailLateRegistrationJob" WHERE state='pending'
                AND (data->>'readyAt')::timestamptz<=%s
                AND (%s::text[] IS NULL OR (id=ANY(%s::text[]) AND data->>'generation'='selected'))
                ORDER BY "updatedAt",id LIMIT 100''',
                (now or datetime.now(timezone.utc),job_ids,job_ids)).fetchall()

    def save_job(self, job, state='pending'):
        with db._connect() as conn:
            conn.execute('''UPDATE "GmailLateRegistrationJob" SET data=%s,state=%s,"updatedAt"=NOW()
                WHERE id=%s AND state='pending' ''',(Jsonb(job['data']),state,job['id']))


def discover(store, client, reps, now, *, deadline=None, clock=time.monotonic):
    """1ページを20件ずつ確定し、時間切れ時はページ内の続きから再開する。"""
    from copy import deepcopy
    state = store.state()
    if not state.get('cutoff'):
        state = dict(initial=True,cutoff=now.replace(second=0,microsecond=0).isoformat(),cursor=None,watermark=None,roundCount=0)
        store.save_state(state)
    if deadline is not None and clock()>=deadline:
        return 0
    if 'pendingPages' not in state:
        body = dict(page_size=PAGE_SIZE,sorts=[dict(timestamp='created_time',direction='ascending')])
        if state.get('cursor'):
            body['start_cursor'] = state['cursor']
        if state.get('watermark'):
            body['filter'] = dict(timestamp='created_time',created_time=dict(on_or_after=state['watermark']))
        try:
            response = client.query_raw(body)
        except Exception as exc:
            if getattr(exc,'status_code',None)==400 and state.get('cursor'):
                state.update(cursor=None,roundCount=0,cursorResets=state.get('cursorResets',0)+1)
                store.save_state(state)
            raise
        pages = response.get('results')
        if not isinstance(pages,list) or len(pages)>PAGE_SIZE or not isinstance(response.get('has_more'),bool):
            raise ValueError('連絡先巡回の応答が不正です')
        next_state=deepcopy(state)
        count=state.get('roundCount',0)+len(pages)
        if response['has_more'] and count<9000:
            if not response.get('next_cursor') or response['next_cursor']==state.get('cursor'):
                raise ValueError('連絡先巡回の続きが不明です')
            next_state.update(cursor=response['next_cursor'],roundCount=count)
        elif response['has_more'] or (response.get('request_status') or {}).get('type')=='incomplete' or count>=9000:
            newest=pages[-1]['created_time'] if pages else None
            if not newest or newest==state.get('watermark'):
                raise ValueError('連絡先巡回の境界が停滞しています')
            next_state.update(cursor=None,watermark=newest,roundCount=0)
        else:
            next_state.update(initial=False,cursor=None,watermark=None,roundCount=0,lastCompletedAt=now.isoformat())
        # ページ内再開に要るメール・時刻だけ。本文・名前等の原本は複製しない。
        state['pendingPages']=[dict(id=p['id'],created_time=p['created_time'],last_edited_time=p['last_edited_time'],
            archived=not source_active(p),properties={EMAIL_PROPERTY:dict(email=email_of(p))}) for p in pages]
        state['pendingObservedAt']=now.isoformat()
        state['nextState']=next_state
        store.save_state(state)
    processed=0
    while state['pendingPages']:
        if deadline is not None and clock()>=deadline:
            return processed
        chunk=state['pendingPages'][:20]
        next_state=deepcopy(state)
        next_state['pendingPages']=state['pendingPages'][20:]
        next_state['lastProgressAt']=now.isoformat()
        next_state.pop('error',None)
        if not next_state['pendingPages']:
            next_state=state['nextState']
            next_state['lastProgressAt']=now.isoformat()
            next_state.pop('error',None)
        store.observe_batch(chunk,reps,utc(state['pendingObservedAt']),utc(state['cutoff']),state['initial'],next_state)
        processed+=len(chunk)
        state=next_state
        if 'pendingPages' not in state:
            return processed
    # 空ページも取得済みの次状態へ進める。
    next_state=state['nextState']
    next_state['lastProgressAt']=now.isoformat()
    next_state.pop('error',None)
    store.save_state(next_state)
    return processed


def merge_block_reason(page_id):
    from src.record_merge.aliases import resolve_alias, require_record_available
    from src.record_merge.domain import MergeHeld
    from src.sync_engine.record_sync_lock import RecordSyncBusy
    try:
        if resolve_alias('contact','notion',page_id):
            return '統合済み旧連絡先のため過去メール取込を保留しています'
        require_record_available('contact',page_id)
    except (MergeHeld,RecordSyncBusy):
        return '連絡先の統合が保留または進行中です'
    return None


def record_guard(page_id):
    from src.sync_engine.record_sync_lock import acquire_record_sync_locks
    # DBを使う本機能ではSQLiteの代替鍵を選ばず、既存の非pooled共有鍵を使う。
    return acquire_record_sync_locks(None,{('contact',page_id)})


def process_job(store, job, client, now, *, connection_reader=db.find_connection_by_email,
                refresh=gmail_client.refresh_access_token, decrypt=decrypt_token,
                list_page=gmail_client.list_messages_page, fetch=gmail_client.get_message,
                exists=db.email_log_exists, insert=db.insert_email_logs, deadline=None):
    """1ページ10通以内。ID確定→各通保存→しおり前進の順で再開する。"""
    if job['state'] != 'pending' or utc(job['data']['readyAt'])>now:
        return 'waiting'
    if deadline is not None and time.monotonic()>=deadline:
        return 'pending'
    data=job['data']
    try:
        page=client.get_raw_page(data['pageId'])
    except Exception as exc:
        if getattr(exc,'status_code',None)!=404:
            raise
        data['reason']='連絡先ページを取得できません（404）'
        store.save_job(job,'blocked')
        return 'blocked'
    reason=merge_block_reason(data['pageId'])
    if reason:
        data['reason']=reason
        store.save_job(job,'blocked')
        return 'blocked'
    unique=client.query_raw(dict(page_size=2,filter=dict(property=EMAIL_PROPERTY,email=dict(equals=data['email']))))
    candidates=unique.get('results',[])
    if (not source_active(page) or email_of(page)!=data['email'] or unique.get('has_more')
            or len(candidates)!=1 or candidates[0]['id']!=data['pageId']):
        data['reason']='連絡先メールの変更・削除・重複候補を確認してください'
        store.save_job(job,'blocked')
        return 'blocked'
    connection=connection_reader(data['rep'])
    if connection is None:
        data['reason']='担当者のGmail連携がありません'
        store.save_job(job,'blocked')
        return 'blocked'
    access=refresh(decrypt(connection.refresh_token_enc))
    if data['pending'] is None:
        try:
            result=list_page(access,query=query_for(data),page_token=data['token'],max_results=MESSAGE_LIMIT)
        except gmail_client.GmailApiError as exc:
            if exc.status_code==400 and data['token']:
                data['token']=None
                data['tokenResets']=data.get('tokenResets',0)+1
                data['reason']='ページの有効期限切れ。保存済みIDを除外して同じ期間から再開します'
                store.save_job(job)
            raise
        if len(result.refs)>MESSAGE_LIMIT or (result.next_page_token and result.next_page_token==data['token']):
            raise ValueError('Gmailのページが上限超過または停滞しています')
        data['pending']=[ref.id for ref in result.refs]
        data['nextToken']=result.next_page_token
        store.save_job(job)
    for message_id in list(data['pending']):
        if deadline is not None and time.monotonic()>=deadline:
            return 'pending'
        if exists(message_id):
            data['existing']+=1
        else:
            try:
                message=fetch(access,message_id)
            except gmail_client.GmailApiError as exc:
                if exc.status_code !=404:
                    raise
                if message_id not in data['missing']:
                    data['missing'].append(message_id)
            else:
                classified=sync.classify_message(message,rep_email=data['rep'],
                    internal_domains=sync.internal_domains_from_env(),
                    resolve_contact=lambda address: data['pageId'] if address==data['email'] else None)
                if classified and data['after']<=classified.sent_at.timestamp()<data['before']:
                    with record_guard(data['pageId']):
                        reason=merge_block_reason(data['pageId'])
                        if reason:
                            data['reason']=reason
                            store.save_job(job,'blocked')
                            return 'blocked'
                        fresh=client.get_raw_page(data['pageId'])
                        if not source_active(fresh) or email_of(fresh)!=data['email']:
                            data['reason']='処理中に連絡先のメールが変わりました'
                            store.save_job(job,'blocked')
                            return 'blocked'
                        data['inserted']+=insert([db.EmailLogRow(contact_page_id=classified.contact_page_id,
                            contact_email=classified.contact_email,rep_email=data['rep'],gmail_message_id=message.id,
                            direction=classified.direction,subject=message.subject,snippet=message.snippet,
                            sent_at=classified.sent_at,gmail_thread_id=classified.thread_id)])
                data['fetched']+=1
        data['pending'].remove(message_id)
        data['lastProgressAt']=now.isoformat()
        data.pop('failedAt',None)
        store.save_job(job)
    data['token']=data.pop('nextToken',None)
    data['pending']=None
    data['reason']=''
    data['lastProgressAt']=now.isoformat()
    data.pop('failedAt',None)
    status='pending' if data['token'] else 'blocked' if data['missing'] else 'done'
    if status=='blocked':
        data['reason']='検索で見つかったメールを取得できません（404）。消失IDの確認が必要です'
    store.save_job(job,status)
    return status


def run(*, job_ids=None):
    """既定無効。import・無効cronはDBやGmailに触れない。"""
    if os.environ.get('GMAIL_LATE_REGISTRATION_ENABLED')!='true':
        return dict(status='disabled')
    from src.db_schema.registry import get_schema
    from src.sync_engine.clients.notion_client import HttpNotionClient
    lock=db.try_acquire_push_sync_lock('late-registration-worker')
    if lock is None:
        return dict(status='busy')
    store=Store()
    now=datetime.now(timezone.utc)
    deadline=time.monotonic()+180
    try:
        client=HttpNotionClient('contact',get_schema('contact').notion_database_id)
        reps=[item.rep_email for item in db.list_gmail_connections()]
        discovered=0
        if job_ids is None:
            discovered=discover(store,client,reps,now,deadline=deadline)
        else:
            # 限定検証では巡回の位置とbaselineを動かさない。
            if not job_ids or len(job_ids)>100 or any(not re.fullmatch('[0-9a-f]{64}',item) for item in job_ids):
                raise ValueError('限定検証には投入結果のジョブIDを指定してください')
        mode=os.environ.get('GMAIL_LATE_REGISTRATION_MODE','baseline')
        processed=failed=busy=0
        if mode not in {'baseline','active'}:
            raise ValueError('遅延登録取込の運転モードが不正です')
        if (mode=='active' and not store.state().get('initial',True)) or job_ids is not None:
            from src.sync_engine.record_sync_lock import RecordSyncBusy
            for job in store.jobs(job_ids,now=now):
                if time.monotonic()>=deadline:
                    break
                try:
                    with gmail_client.bounded_rate_limit_retries(0):
                        process_job(store,job,client,now,deadline=deadline)
                except RecordSyncBusy:
                    busy+=1
                    job['data']['reason']='通常同期の完了後に再試行します'
                    store.save_job(job)
                except Exception:
                    failed+=1
                    job['data']['reason']='過去メール取込に失敗。進捗を保持して再試行します'
                    job['data']['failedAt']=now.isoformat()
                    store.save_job(job)
                processed+=1
        return dict(status='partial_failure' if failed else 'ok',mode=mode,
                    discovered=discovered,processed=processed,failed=failed,busy=busy)

    except Exception:
        state=store.state()
        state['error']='巡回または取込が失敗。完了扱いにせず再試行します'
        state['errorAt']=now.isoformat()
        store.save_state(state)
        raise
    finally:
        db.release_push_sync_lock(lock,'late-registration-worker')


def enqueue_selected(page_ids, *, after, before, client, now=None, apply=False):
    """承認済み1〜5ページだけを試算/投入。既存doneを再開しない。

    beforeは2日以上前・365日以内の明示境界。限定検証でも待機を迂回しない。
    """
    now=now or datetime.now(timezone.utc)
    before=utc(before)
    after=utc(after)
    if not 1<=len(set(page_ids))<=5 or not now-timedelta(days=365)<=after<before<=now-timedelta(days=2):
        raise ValueError('限定対象数または過去取込の境界が不正です')
    connections=db.list_gmail_connections()
    proposed=[]
    for page_id in sorted(set(page_ids)):
        page=client.get_raw_page(page_id)
        email=email_of(page)
        if not source_active(page) or not email:
            raise ValueError('限定対象の連絡先メールを確認できません')
        for rep in connections:
            identity=f'selected:{page_id}:{email}:{after.isoformat()}:{before.isoformat()}:{rep.rep_email}'
            key=hashlib.sha256(identity.encode()).hexdigest()
            payload=dict(pageId=page_id,email=email,rep=rep.rep_email,generation='selected',
                after=int(after.timestamp()),before=int(before.timestamp()),
                readyAt=now.isoformat(),token=None,pending=None,fetched=0,inserted=0,existing=0,missing=[],reason='')
            proposed.append((key,page_id,payload))
    if apply:
        if os.environ.get('GMAIL_LATE_REGISTRATION_ENABLED')!='true':
            raise ValueError('遅延登録の過去取込が無効です')
        with db._connect() as conn:
            for key,page_id,payload in proposed:
                conn.execute('''INSERT INTO "GmailLateRegistrationJob" (id,"pageId",state,data)
                    VALUES (%s,%s,'pending',%s) ON CONFLICT DO NOTHING''',(key,page_id,Jsonb(payload)))
    return dict(pages=len(set(page_ids)),jobs=len(proposed),apply=apply,ids=[item[0] for item in proposed])


def review_held_job(job_id, *, client, action=None, actor='', reason='', expected_fingerprint=None):
    """候補/保留1件を試算し、同じ現物を確認した明示判断だけ監査付きで適用する。

    actionなしは読み取り。retry/dismissは試算fingerprint、判断者、根拠が必須。
    完了・処理中・取下げ済みは再開不可。元の期間/2日待機は変えない。
    """
    if not re.fullmatch('[0-9a-f]{64}',job_id):
        raise ValueError('ジョブIDが不正です')
    if action not in {None,'retry','dismiss'}:
        raise ValueError('判断操作が不正です')
    if action and (not actor.strip() or not reason.strip() or not expected_fingerprint):
        raise ValueError('試算指紋・判断者・根拠を指定してください')
    lock=None
    if action:
        if os.environ.get('GMAIL_LATE_REGISTRATION_ENABLED')!='true':
            raise ValueError('遅延登録の過去取込が無効です')
        lock=db.try_acquire_push_sync_lock('late-registration-worker')
        if lock is None:
            raise ValueError('取込が進行中です。再試算してください')
    try:
        with db._connect() as conn:
            row=conn.execute('SELECT * FROM "GmailLateRegistrationJob" WHERE id=%s FOR UPDATE',
                             (job_id,)).fetchone()
            if row is None or row['state'] not in {'candidate','blocked'}:
                raise ValueError('候補または保留のジョブだけを判断できます')
            with record_guard(row['pageId']):
                try:
                    page=client.get_raw_page(row['pageId'])
                except Exception as exc:
                    if getattr(exc,'status_code',None)!=404:
                        raise
                    source=dict(missing=True)
                else:
                    unique=client.query_raw(dict(page_size=2,filter=dict(property=EMAIL_PROPERTY,
                        email=dict(equals=row['data']['email']))))
                    source=dict(email=email_of(page),active=source_active(page),
                        candidates=sorted(p['id'] for p in unique.get('results',[])),
                        has_more=unique.get('has_more',True),merge=merge_block_reason(row['pageId']))
                evidence=dict(state=row['state'],data=row['data'],source=source)
                fingerprint=hashlib.sha256(json.dumps(evidence,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                eligible=(not source.get('missing') and source.get('active') and
                    source.get('email')==row['data']['email'] and not source.get('merge') and
                    not source.get('has_more') and source.get('candidates')==[row['pageId']])
                if action:
                    if fingerprint!=expected_fingerprint:
                        raise ValueError('試算後にジョブまたは連絡先が変化しました。再試算してください')
                    if action=='retry' and not eligible:
                        raise ValueError('現在の連絡先の一意性・メール・統合状態を確認できません')
                    data=row['data']
                    data.setdefault('decisions',[]).append(dict(action=action,actor=actor,reason=reason,
                        at=datetime.now(timezone.utc).isoformat(),fingerprint=fingerprint,source=source,
                        priorState=row['state'],priorReason=data.get('reason',''),missing=list(data.get('missing',[]))))
                    if action=='retry' and data.get('missing'):
                        # 確定消失したIDだけ再確認し、既に完了したIDは再走査しない。
                        if data.get('pending') is None:
                            data['nextToken']=data.get('token')
                        data['pending']=list(dict.fromkeys(data['missing']+(data.get('pending') or [])))
                        data['missing']=[]
                    data['reason']='明示判断により再試行します' if action=='retry' else '明示判断により取下げました'
                    data.pop('failedAt',None)
                    conn.execute('UPDATE "GmailLateRegistrationJob" SET state=%s,data=%s,"updatedAt"=NOW() WHERE id=%s',
                        ('pending' if action=='retry' else 'dismissed',Jsonb(data),job_id))
                return dict(state=('pending' if action=='retry' else 'dismissed') if action else row['state'],
                            prior_state=row['state'],fingerprint=fingerprint,retry_eligible=eligible,applied=action)
    finally:
        if lock is not None:
            db.release_push_sync_lock(lock,'late-registration-worker')


def health(now=None):
    """本文・宛先・個別IDを出さず、候補件数と実際の停滞を分ける。"""
    now=now or datetime.now(timezone.utc)
    from src.diagnostics.webhook_health import check
    if os.environ.get('GMAIL_LATE_REGISTRATION_ENABLED')!='true':
        return check('gmail_late_registration','ok','遅延登録の過去取込は明示的に無効です')
    with db._connect() as conn:
        row=conn.execute('SELECT data FROM "GmailContactDiscovery" WHERE key=%s',(STATE_KEY,)).fetchone()
        rows=conn.execute('SELECT id,state,data,"createdAt" FROM "GmailLateRegistrationJob" '
                         "WHERE state NOT IN ('done','dismissed') ORDER BY \"createdAt\",id").fetchall()
    state=row['data'] if row else {}
    mode=os.environ.get('GMAIL_LATE_REGISTRATION_MODE','baseline')
    candidates=[r for r in rows if r['state']=='candidate']
    due=[r for r in rows if r['state']=='pending' and utc(r['data']['readyAt'])<=now]
    blocked=[r for r in rows if r['state']=='blocked']
    # 保留は最古の指紋を維持する。新規追加で古い保留の警告年齢をリセットしない。
    stalled=[r for r in due if r['data'].get('failedAt') or now-utc(
        r['data'].get('lastProgressAt') or r['data']['readyAt'])>timedelta(minutes=30)] if mode=='active' else []
    problems=sorted(blocked+stalled,key=lambda r:(r['createdAt'],r['id']))
    status_info=dict(mode=mode,baseline_complete=not state.get('initial',True),candidate_count=len(candidates),
                     blocked_count=len(blocked),due_count=len(due),waiting_count=len(rows)-len(candidates)-len(blocked)-len(due))
    progress=utc(state['lastProgressAt']) if state.get('lastProgressAt') else None
    if not progress or now-progress>timedelta(minutes=30) or state.get('error'):
        return check('gmail_late_registration','unknown','連絡先巡回の停滞・失敗を確認してください',count=len(problems),**status_info)
    if problems:
        return check('gmail_late_registration','pending','遅延登録の過去メール取込の保留・停滞を確認してください',
            count=len(problems),activity_at=problems[0]['createdAt'].isoformat(),
            fingerprint=hashlib.sha256(problems[0]['id'].encode()).hexdigest(),**status_info)
    return check('gmail_late_registration','ok','候補は判断待ち。取込は待機期間内・進行中または完了です',
        last_progress_at=progress.isoformat(),**status_info)
