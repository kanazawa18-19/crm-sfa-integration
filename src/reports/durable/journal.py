"""ページ保存としおりは同じトランザクションで進める。"""
from psycopg.types.json import Jsonb
from src.sync_operations.product_holds import connect
from src.reports.durable.snapshot import report_snapshot


class ReportJournal:
    def detect_missing_dates(self, today):
        # 運用開始後の直近7日だけ検出。当時のデータは再現できないため自動配信しない。
        with connect() as conn:
            conn.execute('''INSERT INTO "ReportCollection" ("reportDate",cutoff,phase,error)
                SELECT missing::date, missing AT TIME ZONE 'Asia/Tokyo', 'held',
                    '当日の収集が開始されていません。過去の実データと送達を確認してください（自動配信なし）'
                FROM generate_series(
                    GREATEST((SELECT min("reportDate") + 1 FROM "ReportCollection"), %s::date - 7),
                    %s::date - 1, interval '1 day') AS missing
                WHERE EXISTS (SELECT 1 FROM "ReportCollection")
                ON CONFLICT DO NOTHING''', (today, today))

    def ensure(self, report_date, cutoff):
        with connect() as conn:
            conn.execute('INSERT INTO "ReportCollection" ("reportDate",cutoff) VALUES (%s,%s) ON CONFLICT DO NOTHING', (report_date, cutoff))

    def next_job(self):
        with connect() as conn:
            return conn.execute('SELECT * FROM "ReportCollection" WHERE phase NOT IN (\'done\',\'held\') ORDER BY "reportDate" LIMIT 1').fetchone()

    def get(self, report_date):
        with connect() as conn:
            return conn.execute('SELECT * FROM "ReportCollection" WHERE "reportDate"=%s', (report_date,)).fetchone()

    def save_page(self, job, pages, cursor, complete):
        phase = ('action' if job['phase'] == 'project' else 'ready') if complete else job['phase']
        with connect() as conn:
            with conn.cursor() as cur:
                cur.executemany('''INSERT INTO "ReportCollectionPage" ("reportDate","dbKey","pageId",page) VALUES (%s,%s,%s,%s)
                    ON CONFLICT ("reportDate","dbKey","pageId") DO UPDATE SET page=EXCLUDED.page''',
                    [(job['reportDate'], job['phase'], page['id'], Jsonb(report_snapshot(job['phase'], page))) for page in pages])
            conn.execute('UPDATE "ReportCollection" SET phase=%s,cursor=%s,error=NULL,"retryCount"=0,"updatedAt"=now() WHERE "reportDate"=%s',
                         (phase, Jsonb({} if complete else cursor), job['reportDate']))

    def pages(self, report_date, db_key):
        with connect() as conn:
            rows = conn.execute('SELECT page FROM "ReportCollectionPage" WHERE "reportDate"=%s AND "dbKey"=%s ORDER BY "pageId"', (report_date, db_key)).fetchall()
            return [row['page'] for row in rows]

    def delivery(self, report_date, kind):
        with connect() as conn:
            return conn.execute('SELECT * FROM "ReportDelivery" WHERE "reportDate"=%s AND kind=%s', (report_date, kind)).fetchone()

    def reserve(self, report_date, kind, destination_hash, body_hash):
        with connect() as conn:
            conn.execute('INSERT INTO "ReportDelivery" ("reportDate",kind,"destinationHash","bodyHash") VALUES (%s,%s,%s,%s)',
                         (report_date, kind, destination_hash, body_hash))

    def delivered(self, report_date, kind):
        with connect() as conn:
            conn.execute('UPDATE "ReportDelivery" SET state=\'delivered\',"deliveredAt"=now() WHERE "reportDate"=%s AND kind=%s', (report_date, kind))

    def finish(self, report_date):
        with connect() as conn:
            conn.execute('UPDATE "ReportCollection" SET phase=\'done\',"completedAt"=now(),"updatedAt"=now(),error=NULL WHERE "reportDate"=%s', (report_date,))
            # 今回より古い完了分を整理する。収集中・保留中の原本と配送台帳は残す。
            conn.execute('''DELETE FROM "ReportCollectionPage" p USING "ReportCollection" c
                WHERE p."reportDate"=c."reportDate" AND c.phase='done'
                  AND c."reportDate" < %s''', (report_date,))

    def error(self, report_date, reason, *, held=False):
        with connect() as conn:
            conn.execute('UPDATE "ReportCollection" SET error=%s,"retryCount"="retryCount"+1,phase=CASE WHEN %s OR "retryCount">=2 THEN \'held\' ELSE phase END,"updatedAt"=now() WHERE "reportDate"=%s',
                         (reason, held, report_date))
