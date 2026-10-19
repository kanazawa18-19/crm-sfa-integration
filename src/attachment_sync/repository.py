"""直接接続上のセッションロックと、通信前の予約を保存する。"""
from contextlib import contextmanager
from dataclasses import asdict
from psycopg.types.json import Jsonb
from src.contact_name_job.repository import connect
from src.sync_engine.record_sync_lock import lock_key
from src.attachment_sync.domain import Source


class Repository:
    def __init__(self, conn):
        self.conn = conn

    @classmethod
    @contextmanager
    def locked(cls):
        with connect() as conn:
            if not conn.autocommit:raise RuntimeError('添付台帳は自動コミット接続が必要です')
            ok = conn.execute('SELECT pg_try_advisory_lock(%s) ok', (lock_key('attachment_sync', 'main'),)).fetchone()['ok']
            yield cls(conn) if ok else None

    def check(self):
        self.conn.execute('SELECT 1')

    def state(self, key):
        row = self.conn.execute('SELECT data FROM "AttachmentSyncState" WHERE id=%s', (key,)).fetchone()
        return row['data'] if row else {}

    def put_state(self, key, data):
        self.conn.execute('INSERT INTO "AttachmentSyncState" (id,data) VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET data=EXCLUDED.data,"updatedAt"=now()', (key, Jsonb(data)))

    def add(self, source):
        self.conn.execute('INSERT INTO "AttachmentSyncSource" (id,data) VALUES (%s,%s) ON CONFLICT(id) DO NOTHING', (source.key, Jsonb(asdict(source))))

    def pending(self):
        return self.conn.execute('SELECT * FROM "AttachmentSyncSource" WHERE state IN (\'pending\',\'retry\') AND "nextAttemptAt"<=now() ORDER BY "nextAttemptAt",id LIMIT 1').fetchone()

    def finish(self, key, state, asset_id=None, reason=None):
        self.conn.execute('UPDATE "AttachmentSyncSource" SET state=%s,"assetId"=COALESCE(%s,"assetId"),reason=%s,"nextAttemptAt"=now()+interval \'1 hour\',"updatedAt"=now() WHERE id=%s', (state, asset_id, reason, key))

    def asset(self, key):
        row = self.conn.execute('SELECT data FROM "AttachmentSyncAsset" WHERE id=%s', (key,)).fetchone()
        return row['data'] if row else None

    def save_asset(self, key, data):
        self.conn.execute('INSERT INTO "AttachmentSyncAsset" (id,data) VALUES (%s,%s) ON CONFLICT(id) DO UPDATE SET data=EXCLUDED.data,"updatedAt"=now()', (key, Jsonb(data)))

    def mail_batch(self, after):
        return self.conn.execute('''SELECT r.id,r.data,r."updatedAt",o."ownerEmail",o."gmailMessageId"
          FROM "EmailReview" r JOIN "EmailReviewOwner" o ON o."reviewId"=r.id
          WHERE r.data->>'state'='done' AND r.data->>'action_id' IS NOT NULL
          AND (r."updatedAt",r.id,o."ownerEmail",o."gmailMessageId") > (%s::timestamptz,%s,%s,%s)
          ORDER BY r."updatedAt",r.id,o."ownerEmail",o."gmailMessageId" LIMIT 5''',
          (after.get('at','1970-01-01T00:00:00Z'),after.get('id',''),after.get('owner',''),after.get('message',''))).fetchall()

    def status(self):
        rows = self.conn.execute('SELECT state,count(*) n FROM "AttachmentSyncSource" GROUP BY state').fetchall()
        return {'counts': {r['state']:r['n'] for r in rows}, 'progress': self.state('progress'), 'discovery_errors': self.errors(), 'discovery_error_count': self.conn.execute("SELECT count(*) n FROM \"AttachmentSyncState\" WHERE id LIKE 'error:%' AND data<>'{}'::jsonb").fetchone()['n']}

    def mark_mirror(self, source, asset_id):
        self.add(source)
        self.finish(source.key, 'mirrored', asset_id)

    def errors(self):
        return self.conn.execute('SELECT id,data FROM "AttachmentSyncState" WHERE id LIKE \'error:%\' AND data<>\'{}\'::jsonb LIMIT 20').fetchall()

    def retry_held(self, key):
        # 保存予約は消さない。再試行でも結果不明の追記を作り直さない。
        return self.conn.execute('UPDATE "AttachmentSyncSource" SET state=\'retry\',"nextAttemptAt"=now() WHERE id=%s AND state=\'held\'', (key,)).rowcount

    def next_event(self):
        return self.conn.execute("""SELECT id,data FROM "AttachmentSyncState" WHERE id LIKE 'event:%' AND data<>'{}'::jsonb ORDER BY "updatedAt",id LIMIT 1""").fetchone()

    def complete_event(self, event):
        self.conn.execute("""UPDATE "AttachmentSyncState" SET data='{}'::jsonb WHERE id=%s AND data=%s""", (event['id'],Jsonb(event['data'])))

    def problems(self, offset=0):
        return self.conn.execute('''SELECT id,data->>'name' AS name,data->>'db' AS db,
          data->>'record' AS record,state,reason,"assetId"
          FROM "AttachmentSyncSource" WHERE state IN ('held','retry')
          ORDER BY "updatedAt" DESC,id LIMIT 100 OFFSET %s''', (offset,)).fetchall()

    def find_mirror(self, source, digest):
        # kintoneの取得キーが変化しても、配送先・内容・付与した表示名で複製を特定する。
        rows=self.conn.execute('''SELECT id,data FROM "AttachmentSyncAsset"
          WHERE data->>'db'=%s AND data->>'record'=%s AND data->>'sha256'=%s
          AND data->'receipts'->'kintone'->>'state'='verified' ''',
          (source.db,source.record,digest)).fetchall()
        from pathlib import Path
        from src.attachment_sync.domain import filename
        for row in rows:
            base=Path(filename(row['data']['name']))
            expected=base.stem+' [CRM-'+row['id'][:16]+']'+base.suffix
            if source.name==expected:return row['id']
        return None
