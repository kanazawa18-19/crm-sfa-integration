"""照合ページ完了後のしおり。呼出し側は登録元の同期ロックを保持する。"""
from psycopg.types.json import Jsonb
from src.hub_creation.journal import connect


class PostgresScanJournal:
    def get(self, key):
        with connect() as conn:
            return conn.execute('SELECT * FROM "HubCreationScan" WHERE "sourceKey"=%s', (key,)).fetchone()

    def save(self, context, fingerprint, checkpoint, *, state='pending', reason=''):
        with connect() as conn:
            conn.execute('''INSERT INTO "HubCreationScan"
                ("sourceKey","sourceId","dbKey","inputHash",checkpoint,state,reason,"nextAttemptAt")
                VALUES (%s,%s,%s,%s,%s,%s,%s,now()+interval '1 minute')
                ON CONFLICT ("sourceKey") DO UPDATE SET "sourceId"=EXCLUDED."sourceId",
                "dbKey"=EXCLUDED."dbKey","inputHash"=EXCLUDED."inputHash",checkpoint=EXCLUDED.checkpoint,
                state=EXCLUDED.state,reason=EXCLUDED.reason,"nextAttemptAt"=EXCLUDED."nextAttemptAt","updatedAt"=now()''',
                (context['sourceKey'],context['sourceId'],context['dbKey'],fingerprint,Jsonb(checkpoint),state,reason))

    def due(self, limit=5):
        with connect() as conn:
            return conn.execute('''SELECT s.* FROM "HubCreationScan" s
                LEFT JOIN "HubCreationAttempt" a ON a."sourceKey"=s."sourceKey" AND a.target='zoho'
                WHERE s.state='pending' AND s."nextAttemptAt"<=now()
                  AND (a.state IS NULL OR a.state='blocked')
                ORDER BY s."nextAttemptAt" LIMIT %s''', (limit,)).fetchall()

    def defer(self, key):
        with connect() as conn:
            conn.execute('''UPDATE "HubCreationScan" SET "nextAttemptAt"=now()+interval '10 minutes'
                WHERE "sourceKey"=%s''', (key,))

    def done(self, key):
        with connect() as conn:
            conn.execute('UPDATE "HubCreationScan" SET state=\'done\',"updatedAt"=now() WHERE "sourceKey"=%s', (key,))

    def hold(self, key, reason):
        with connect() as conn:
            conn.execute('''UPDATE "HubCreationScan" SET state='held',reason=%s,"updatedAt"=now()
                WHERE "sourceKey"=%s AND state='pending' ''', (reason, key))
