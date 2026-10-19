"""実行・巡回・判定・未配送を永続化する。"""
from contextlib import contextmanager
from datetime import datetime, timezone
from psycopg.types.json import Jsonb
from src.contact_name_job.repository import connect
from src.sync_engine.record_sync_lock import lock_key


class Repository:
    @contextmanager
    def locked(self):
        with connect() as conn:
            acquired = conn.execute('SELECT pg_try_advisory_lock(%s) AS locked',
                                   (lock_key('contact_affiliation_job', 'main'),)).fetchone()['locked']
            yield conn if acquired else None

    def config(self, conn):
        return conn.execute('SELECT * FROM "ContactAffiliationJob" WHERE id=\'main\'').fetchone()

    def discovery(self, conn, pages, cursor):
        with conn.transaction():
            for page in pages:
                from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value
                props = page.get('properties') or {}
                email = parse_notion_property_value(props['メールアドレス']) if props.get('メールアドレス') else None
                conn.execute('INSERT INTO "ContactAffiliationItem" ("notionId",email) VALUES (%s,%s) '
                             'ON CONFLICT ("notionId") DO UPDATE SET email=EXCLUDED.email',
                             (page['id'], email.strip().casefold() if email else None))
                affiliation = props.get('取引先マスター') or {}
                if affiliation.get('type') == 'relation' and affiliation.get('relation') == [] and not affiliation.get('has_more'):
                    released = conn.execute("""UPDATE \"ContactAffiliationItem\" SET state='released',
                        decision=decision || '{\"reason\":\"所属が解除されました。自動では戻しません\"}'::jsonb, \"updatedAt\"=now()
                        WHERE \"notionId\"=%s AND state IN ('existing','verified') RETURNING decision""", (page['id'],)).fetchone()
                    if released:
                        conn.execute('INSERT INTO "ContactAffiliationHistory" ("notionId",state,decision) VALUES (%s,\'released\',%s)',
                                     (page['id'], Jsonb(released['decision'])))
            conn.execute('UPDATE "ContactAffiliationJob" SET discovery=%s,metrics=metrics || %s WHERE id=\'main\'',
                         (Jsonb(cursor), Jsonb({'last_discovery_completed_at': datetime.now(timezone.utc).isoformat()} if not cursor else {})))

    def next(self, conn):
        return conn.execute('''SELECT * FROM "ContactAffiliationItem" WHERE "nextAttemptAt"<=now() AND state NOT IN ('verified','existing','released')
            ORDER BY "nextAttemptAt","notionId" LIMIT 1''').fetchone()

    def save(self, conn, key, state, decision, error=None):
        with conn.transaction():
            conn.execute('''UPDATE "ContactAffiliationItem" SET state=%s, decision=%s,
                "errorType"=%s,"updatedAt"=now(),"nextAttemptAt"=now()+make_interval(secs=>%s)
                WHERE "notionId"=%s''', (state, Jsonb(decision), error,
                3600 if state in ('writing', 'delivery_pending', 'retry') else (604800 if state == 'held' else 86400), key))
            conn.execute('INSERT INTO "ContactAffiliationHistory" ("notionId",state,decision) VALUES (%s,%s,%s)',
                         (key, state, Jsonb(decision)))

    def status(self):
        with connect() as conn:
            config = self.config(conn)
            errors = conn.execute('SELECT count(*) n FROM "ContactAffiliationItem" WHERE "errorType" IS NOT NULL').fetchone()['n']
            counts = {r['state']: r['n'] for r in conn.execute('SELECT state,count(*) n FROM "ContactAffiliationItem" GROUP BY state').fetchall()}
            return {'enabled': config['enabled'], 'dry_run': config['dryRun'], 'counts': counts,
                    'last_run_at': config['lastRunAt'], 'last_success_at': config['lastSuccessAt'],
                    'unevaluated': counts.get('pending', 0), 'total_discovered': sum(counts.values()), 'metrics': config['metrics'],
                    'last_error': config['lastError'], 'retry_count': errors, 'discovery': config['discovery']}

    def configure(self, *, enabled, dry_run):
        with self.locked() as conn:
            if conn is None:
                raise ValueError('実行中です')
            conn.execute('UPDATE "ContactAffiliationJob" SET enabled=%s,"dryRun"=%s WHERE id=\'main\'', (enabled, dry_run))
            if enabled and not dry_run:
                conn.execute('UPDATE "ContactAffiliationItem" SET "nextAttemptAt"=now() WHERE state=\'candidate\'')

    def mail_rows(self, email):
        # 本文の曖昧検索ではなく、構造化したメールアドレスを持つ受付だけを読む。
        with connect() as conn:
            rows = conn.execute('''SELECT data FROM "EmailReview" WHERE EXISTS
              (SELECT 1 FROM jsonb_array_elements(COALESCE(data->'participants','[]'::jsonb)) p
               WHERE lower(p->>'email')=%s)''', (email,)).fetchall()
            return [r['data'] for r in rows]

    def extra_evidence(self, email):
        with connect() as conn:
            return [r['data'] for r in conn.execute('SELECT data FROM "ContactAffiliationEvidence" WHERE email=%s', (email,)).fetchall()]

    def import_evidence(self, rows):
        from datetime import datetime
        allowed = {'inquiry_form', 'application_form', 'explicit_affiliation_record', 'official_staff_profile'}
        for row in rows:
            if (row.get('source') not in allowed or row.get('explicit') is not True
                    or not all(isinstance(row.get(k), str) and row[k].strip()
                               for k in ('source_id', 'origin', 'email', 'company_name', 'observed_at', 'source_url'))
                    or row['email'] != row['email'].strip().casefold()
                    or '@' not in row['email'] or len(row['company_name']) > 300
                    or datetime.fromisoformat(row['observed_at'].replace('Z', '+00:00')).tzinfo is None):
                raise ValueError('所属を明示する原本・日時・本人メールが必要です')
        with connect() as conn, conn.transaction():
            for row in rows:
                old = conn.execute('SELECT data FROM "ContactAffiliationEvidence" WHERE "sourceId"=%s FOR UPDATE', (row['source_id'],)).fetchone()
                if old and old['data'] != row:
                    raise ValueError('原本IDが同じ異なる根拠です。元の履歴を上書きできません')
                conn.execute('INSERT INTO "ContactAffiliationEvidence" ("sourceId",email,data) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',
                             (row['source_id'], row['email'], Jsonb(row)))


    def prioritize_mail(self, conn):
        """既存のメール→連絡先索引で新着根拠を優先し、全件巡回を待たせない。"""
        cursor = self.config(conn)['mailCursor']
        rows = conn.execute('''SELECT id,data,"updatedAt" FROM "EmailReview"
            WHERE ("updatedAt",id)>(%s::timestamptz,%s) ORDER BY "updatedAt",id LIMIT 200''',
            (cursor.get('time', '1970-01-01T00:00:00Z'), cursor.get('id', ''))).fetchall()
        if not rows:
            return 0
        emails = sorted({p['email'].strip().casefold() for r in rows for p in r['data'].get('participants', [])
                         if isinstance(p, dict) and isinstance(p.get('email'), str) and p['email']})
        with conn.transaction():
            keys = conn.execute('''SELECT "pageId" AS id,lower(email) AS email FROM "GmailContactEmail" WHERE lower(email)=ANY(%s)
                UNION SELECT "notionId" AS id,email FROM "ContactAffiliationItem" WHERE email=ANY(%s)''', (emails, emails)).fetchall()
            from uuid import UUID
            for entry in keys:
                key = str(UUID(entry['id']))
                conn.execute('''INSERT INTO "ContactAffiliationItem" ("notionId",email,"nextAttemptAt") VALUES (%s,%s,now()-interval '1 day')
                    ON CONFLICT ("notionId") DO UPDATE SET "nextAttemptAt"=LEAST("ContactAffiliationItem"."nextAttemptAt",now()-interval '1 day')
                    WHERE "ContactAffiliationItem".state NOT IN ('verified','existing','released')''', (key, entry['email']))
            last = rows[-1]
            conn.execute('UPDATE "ContactAffiliationJob" SET "mailCursor"=%s WHERE id=\'main\'',
                         (Jsonb({'time': last['updatedAt'].isoformat(), 'id': last['id']}),))
        return len(keys)


    def report(self, command, contact_id=None, limit=1000):
        with connect() as conn:
            if command == 'candidates':
                return conn.execute('''SELECT "notionId",state,decision,"updatedAt" FROM "ContactAffiliationItem"
                    WHERE state IN ('candidate','held','writing','delivery_pending','released') ORDER BY "updatedAt" DESC LIMIT %s''', (limit,)).fetchall()
            return conn.execute('''SELECT * FROM "ContactAffiliationHistory" WHERE (%s::text IS NULL OR "notionId"=%s)
                ORDER BY id DESC LIMIT %s''', (contact_id, contact_id, limit)).fetchall()
