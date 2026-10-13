"""非冪等POSTの前に予約を確定する。応答不明は自動で作り直さない。"""
from __future__ import annotations
import os
import psycopg
from psycopg.rows import dict_row


def connect():
    return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=10)


class PostgresCreationJournal:
    def get(self, source_key, target):
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM "HubCreationAttempt" WHERE "sourceKey"=%s AND target=%s', (source_key, target))
            return cur.fetchone()

    def has_source(self, source_key):
        """新規登録の経路を通った印。対応表保存後の中断も再開対象にする。"""
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT 1 FROM "HubCreationAttempt" WHERE "sourceKey"=%s LIMIT 1', (source_key,))
            return cur.fetchone() is not None

    def reserve(self, source_key, target, db_key, fingerprint):
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute('''INSERT INTO "HubCreationAttempt" ("sourceKey", target, "dbKey", "identityHash", state, reason)
                    VALUES (%s,%s,%s,%s,'reserved','作成結果の確認待ち')
                    ON CONFLICT ("sourceKey",target) DO UPDATE SET state='reserved', reason='作成結果の確認待ち',
                    "identityHash"=EXCLUDED."identityHash", "updatedAt"=NOW()
                    WHERE "HubCreationAttempt".state='blocked' RETURNING "sourceKey"''', (source_key,target,db_key,fingerprint))
                return cur.fetchone() is not None
        except psycopg.errors.UniqueViolation:
            return False

    def hold(self, source_key, target, db_key, reason):
        with connect() as conn, conn.cursor() as cur:
            cur.execute('''INSERT INTO "HubCreationAttempt" ("sourceKey",target,"dbKey",state,reason)
                VALUES (%s,%s,%s,'blocked',%s) ON CONFLICT ("sourceKey",target) DO UPDATE
                SET reason=EXCLUDED.reason, "updatedAt"=NOW() WHERE "HubCreationAttempt".state='blocked' ''',
                (source_key,target,db_key,reason))

    def finish(self, source_key, target, external_id):
        with connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE "HubCreationAttempt" SET state='created', "externalId"=%s,
                reason='', "updatedAt"=NOW() WHERE "sourceKey"=%s AND target=%s AND state='reserved' ''',
                (external_id,source_key,target))
            if cur.rowcount != 1:
                raise RuntimeError("作成予約の確定に失敗しました")

    def conflicts(self, db_key, target, external_id):
        with connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT 1 FROM "HubCreationAttempt" WHERE "dbKey"=%s AND target=%s
                AND state='created' AND "externalId"=%s LIMIT 1''', (db_key,target,str(external_id)))
            if cur.fetchone() is not None:
                return True
            # 作成結果不明の間は、新規受信を再送させる。名前が変換されても二重作成しない。
            cur.execute('''SELECT 1 FROM "HubCreationAttempt" WHERE "dbKey"=%s AND target=%s
                AND state='reserved' LIMIT 1''', (db_key, target))
            if cur.fetchone() is not None:
                from src.sync_engine.record_sync_lock import RecordSyncBusy
                raise RecordSyncBusy("外部への作成結果を確認してから新規通知を再処理します")
            return False

    def source_for_page(self, page_id):
        with connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT "sourceKey" FROM "HubCreationAttempt" WHERE target=\'notion\' AND state=\'created\' AND "externalId"=%s', (page_id,))
            rows = cur.fetchall()
            if len(rows) > 1:
                raise RuntimeError("ページの作成予約が重複しています")
            return rows[0]["sourceKey"] if rows else None

    def resolve_hold(self, source_key, target, page_id, *, expected_reason=None):
        # 入口由来の保留を解消する場合、確認後に別理由へ変わっていないことも同時に判定する。
        query = """UPDATE "HubCreationAttempt" SET state='created', reason='', "externalId"=%s,
            "updatedAt"=NOW() WHERE "sourceKey"=%s AND target=%s AND state='blocked'"""
        params = (page_id, source_key, target)
        if expected_reason is not None:
            query += ' AND reason=%s'
            params += (expected_reason,)
        with connect() as conn, conn.cursor() as cur:
            cur.execute(query, params)

    def dismiss_hold(self, source_key, target):
        """対象外になったPOST前の保留だけを取り除く。予約済み・作成済みは消さない。"""
        with connect() as conn, conn.cursor() as cur:
            cur.execute('DELETE FROM "HubCreationAttempt" WHERE "sourceKey"=%s AND target=%s AND state=\'blocked\'', (source_key,target))

    def retire_archived(self, source_key, db_key, page_id):
        """原本のアーカイブ確認後、POST前の保留を監査記録へ移す。"""
        from uuid import uuid4
        from psycopg.types.json import Jsonb
        with connect() as conn:
            rows = conn.execute('''DELETE FROM "HubCreationAttempt"
                WHERE "sourceKey"=%s AND "dbKey"=%s AND state='blocked' RETURNING *''',
                (source_key, db_key)).fetchall()
            if rows:
                # 消去と証跡を同一トランザクションで確定する。作成予約は維持する。
                before = [{key: str(value) if key.endswith('At') and value is not None else value
                           for key, value in row.items()} for row in rows]
                conn.execute('''INSERT INTO "AuditLog"
                    (id,"dbKey","notionPageId",action,"changedFields","actorSource")
                    VALUES (%s,%s,%s,'creation_hold_retired',%s,'hub_creation')''',
                    (str(uuid4()),db_key,page_id,Jsonb({
                        '新規作成保留': {'before':before,'after':[]},
                        '退避理由': {'before':None,'after':'原本がアーカイブ済み'},
                        '登録元': {'before':source_key,'after':source_key}})))

    def pending_counts(self):
        with connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT target, state, count(*) AS count FROM "HubCreationAttempt"
                WHERE state <> 'created' GROUP BY target,state ORDER BY target,state''')
            return cur.fetchall()

    def pending_details(self):
        with connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT "sourceKey",target,state,reason FROM "HubCreationAttempt"
                WHERE state <> 'created' ORDER BY "createdAt" LIMIT 10''')
            return cur.fetchall()
