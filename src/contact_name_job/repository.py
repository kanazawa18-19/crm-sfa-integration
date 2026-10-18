"""直接接続とセッションロックで、期限切れによる二重実行を避ける。"""
import os
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.contact_name_job.domain import JOB_ID, validate_plan
from src.sync_engine.record_sync_lock import lock_key


def connect():
    from psycopg.conninfo import conninfo_to_dict
    url = os.environ['DATABASE_URL_UNPOOLED']
    if '-pooler' in conninfo_to_dict(url).get('host', ''):
        raise ValueError('直接接続が必要です')
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=10,
                           options='-c statement_timeout=10000 -c idle_session_timeout=180000',
                           keepalives=1, keepalives_idle=30, keepalives_interval=10,
                           keepalives_count=3, autocommit=True)


@contextmanager
def record_lock(db_key, key):
    from src.sync_engine.record_sync_lock import RecordSyncBusy
    with connect() as conn:
        if not conn.execute('SELECT pg_try_advisory_lock(%s) AS locked', (lock_key(db_key,key),)).fetchone()['locked']:
            raise RecordSyncBusy('同じ連絡先を処理中です')
        # 自動再接続はしない。期限切れの古い実行は書込み前に失敗する。
        yield lambda: conn.execute('SELECT 1')


class Repository:
    @contextmanager
    def shard(self, number):
        if number not in range(4):
            raise ValueError('処理番号が不正です')
        with connect() as conn:
            acquired = conn.execute('SELECT pg_try_advisory_lock(%s) AS locked',
                                    (lock_key('contact_name_job', f'{JOB_ID}:{number}'),)).fetchone()['locked']
            yield conn if acquired else None

    def active(self, conn):
        row = conn.execute('SELECT enabled FROM "ContactNameJob" WHERE id=%s', (JOB_ID,)).fetchone()
        return bool(row and row['enabled'])

    def next(self, conn, shard):
        return conn.execute('''SELECT plan, attempts FROM "ContactNameJobItem"
            WHERE "jobId"=%s AND shard=%s AND state IN ('pending','retry')
            AND "nextAttemptAt"<=now() ORDER BY ordinal LIMIT 1''', (JOB_ID, shard)).fetchone()

    def started(self, conn):
        conn.execute('UPDATE "ContactNameJob" SET "lastRunAt"=now() WHERE id=%s', (JOB_ID,))

    def finish(self, conn, entry, state, error_type=None, *, count_attempt=True):
        with conn.transaction():
            conn.execute('''UPDATE "ContactNameJobItem" SET
                state=CASE WHEN %s='retry' AND %s AND attempts>=7 THEN 'failed' ELSE %s END,
                attempts=attempts+%s, "errorType"=%s, "updatedAt"=now(),
                "nextAttemptAt"=now()+make_interval(secs=>LEAST(1800,300*(attempts+1)))
                WHERE "jobId"=%s AND "notionId"=%s''',
                (state, count_attempt, state, int(count_attempt), error_type, JOB_ID, entry['id']))
            if state == 'verified':
                conn.execute('UPDATE "ContactNameJob" SET "lastSuccessAt"=now() WHERE id=%s', (JOB_ID,))

    def status(self):
        with connect() as conn:
            job = conn.execute('SELECT enabled,"lastRunAt","lastSuccessAt" FROM "ContactNameJob" WHERE id=%s', (JOB_ID,)).fetchone()
            if not job:
                return {'status': 'not_imported', 'total': 0, 'counts': {}}
            rows = conn.execute('SELECT state,count(*) AS n FROM "ContactNameJobItem" WHERE "jobId"=%s GROUP BY state', (JOB_ID,)).fetchall()
            counts = {state: 0 for state in ('pending','retry','verified','held','failed')}
            counts.update({r['state']: r['n'] for r in rows})
            remaining = counts['pending'] + counts['retry']
            status = ('running' if job['enabled'] else 'paused') if remaining else (
                'needs_attention' if counts['held'] + counts['failed'] else 'completed')
            return {'status': status, 'enabled': job['enabled'], 'total': sum(counts.values()),
                    'remaining': remaining, 'counts': counts,
                    'last_run_at': job['lastRunAt'], 'last_success_at': job['lastSuccessAt']}

    def import_plan(self, entries, verified):
        digest = validate_plan(entries)
        if not set(verified) <= {e['id'] for e in entries}:
            raise ValueError('計画外の保存確認IDがあります')
        with connect() as conn, conn.transaction():
            conn.execute('SELECT pg_advisory_xact_lock(%s)', (lock_key('contact_name_job', 'import'),))
            old = conn.execute('SELECT "planHash" FROM "ContactNameJob" WHERE id=%s', (JOB_ID,)).fetchone()
            if old:
                if old['planHash'] != digest:
                    raise ValueError('既存計画との差替えはできません')
                return  # 再実行で既存の進捗や保留を巻き戻さない。
            conn.execute('INSERT INTO "ContactNameJob" (id,"planHash") VALUES (%s,%s)', (JOB_ID,digest))
            with conn.cursor() as cur:
                cur.executemany('''INSERT INTO "ContactNameJobItem"
                    ("jobId","notionId",ordinal,shard,plan,state) VALUES (%s,%s,%s,%s,%s,%s)''',
                    [(JOB_ID,e['id'],i,i%4,Jsonb(e),'verified' if e['id'] in verified else 'pending') for i,e in enumerate(entries)])

    def set_enabled(self, enabled):
        with connect() as conn:
            if conn.execute('UPDATE "ContactNameJob" SET enabled=%s WHERE id=%s', (enabled,JOB_ID)).rowcount != 1:
                raise ValueError('計画が未登録です')
