"""TODOの非冪等作成と更新前の状態を既存予約表へ保存する。本文は台帳に複写しない。"""
from __future__ import annotations
import hashlib
import json
import psycopg
from src.hub_creation.journal import PostgresCreationJournal, connect
from src.sync_engine.record_sync_lock import acquire_record_sync_lock


class TodoJournal:
    target = 'notion_todo'

    def lock(self, key):
        return acquire_record_sync_lock(object(), 'project', key)

    def get(self, key):
        row = PostgresCreationJournal().get(key, self.target)
        if row is None:
            return None
        return {**row, 'metadata': json.loads(row['reason'])}

    def reserve(self, key, digest):
        metadata = json.dumps({'baseline': digest, 'pending': None})
        try:
            with connect() as conn:
                row = conn.execute('''INSERT INTO "HubCreationAttempt"
                    ("sourceKey",target,"dbKey","identityHash",state,reason)
                    VALUES (%s,%s,'project',%s,'reserved',%s)
                    ON CONFLICT ("sourceKey",target) DO NOTHING RETURNING "sourceKey"''',
                    (key, self.target, hashlib.sha256(key.encode()).hexdigest(), metadata)).fetchone()
            return row is not None
        except psycopg.errors.UniqueViolation:
            return False

    def finish(self, key, page_id):
        with connect() as conn:
            cursor = conn.execute('''UPDATE "HubCreationAttempt" SET state='created', "externalId"=%s,
                "updatedAt"=NOW() WHERE "sourceKey"=%s AND target=%s AND state='reserved' ''',
                (page_id, key, self.target))
            if cursor.rowcount != 1:
                raise RuntimeError('TODO作成IDを確定できません')
            self._hold(conn, key, 'TODOの保存値を確認しています')

    def metadata(self, key, baseline, pending=None):
        with connect() as conn:
            cursor = conn.execute('''UPDATE "HubCreationAttempt" SET reason=%s,"updatedAt"=NOW()
                WHERE "sourceKey"=%s AND target=%s AND state='created' ''',
                (json.dumps({'baseline': baseline, 'pending': pending}), key, self.target))
            if cursor.rowcount != 1:
                raise RuntimeError('TODOの更新状態を保存できません')
            if pending is not None:
                self._hold(conn, key, 'TODO更新の保存値を確認しています')

    def accept_source(self, key, source_at, source_hash):
        """案件ロック内で原本版を先に確定する。古いスナップショットは外部へ送らない。"""
        from datetime import datetime
        with connect() as conn:
            conn.execute('''INSERT INTO "HubCreationAttempt" ("sourceKey",target,"dbKey",state,reason)
                VALUES (%s,'calendar_task_batch','project','created','{}')
                ON CONFLICT ("sourceKey",target) DO NOTHING''', (key,))
            row = conn.execute('''SELECT reason FROM "HubCreationAttempt"
                WHERE "sourceKey"=%s AND target='calendar_task_batch' FOR UPDATE''', (key,)).fetchone()
            saved = json.loads(row['reason'])
            accepted = saved.get('accepted_source_at')
            if accepted:
                incoming, previous = datetime.fromisoformat(source_at), datetime.fromisoformat(accepted)
                if incoming < previous:
                    return 'stale'
                if incoming == previous:
                    return 'accepted' if saved.get('accepted_source_hash') == source_hash else 'conflict'
            saved.update(accepted_source_at=source_at, accepted_source_hash=source_hash, plan_hash=None, next_index=0)
            conn.execute('''UPDATE "HubCreationAttempt" SET reason=%s,"updatedAt"=NOW()
                WHERE "sourceKey"=%s AND target='calendar_task_batch' ''', (json.dumps(saved), key))
        return 'accepted'

    def batch_position(self, key, plan_hash):
        row = PostgresCreationJournal().get(key, 'calendar_task_batch')
        if row is None:
            return 0
        saved = json.loads(row['reason'])
        return saved['next_index'] if saved.get('plan_hash') == plan_hash else 0

    def batch_advance(self, key, plan_hash, next_index):
        with connect() as conn:
            conn.execute('''INSERT INTO "HubCreationAttempt" ("sourceKey",target,"dbKey",state,reason)
                VALUES (%s,'calendar_task_batch','project','created',%s)
                ON CONFLICT ("sourceKey",target) DO UPDATE
                SET reason=("HubCreationAttempt".reason::jsonb || EXCLUDED.reason::jsonb)::text,"updatedAt"=NOW()''',
                (key, json.dumps({'plan_hash': plan_hash, 'next_index': next_index})))

    def calendar_get(self, key):
        return PostgresCreationJournal().get(key, 'calendar_task_event')

    def calendar_reserve(self, key, request_hash):
        with connect() as conn:
            row = conn.execute('''INSERT INTO "HubCreationAttempt"
                ("sourceKey",target,"dbKey",state,reason)
                VALUES (%s,'calendar_task_event','project','reserved',%s)
                ON CONFLICT ("sourceKey",target) DO UPDATE SET state='reserved',
                reason=EXCLUDED.reason,"updatedAt"=NOW()
                WHERE "HubCreationAttempt".state IN ('created','blocked') RETURNING "sourceKey"''',
                (key, 'カレンダー応答確認待ち。結果不明時は手動照合まで再実行しません。 request_hash=' + request_hash)).fetchone()
        return row is not None

    def calendar_finish(self, key, *, event_id=None, skipped=None):
        with connect() as conn:
            cursor = conn.execute('''UPDATE "HubCreationAttempt" SET state=%s, "externalId"=%s,
                reason=%s,"updatedAt"=NOW() WHERE "sourceKey"=%s AND target='calendar_task_event'
                AND state='reserved' ''',
                ('blocked' if skipped else 'created', event_id, skipped or '', key))
            if cursor.rowcount != 1:
                raise RuntimeError('カレンダー応答結果を確定できません')

    def _hold(self, conn, key, reason, page_id=None):
        conn.execute('''INSERT INTO "HubCreationAttempt" ("sourceKey",target,"dbKey",state,reason,"externalId")
            VALUES (%s,'notion_todo_review','project','blocked',%s,%s)
            ON CONFLICT ("sourceKey",target) DO UPDATE SET reason=EXCLUDED.reason,
            "externalId"=COALESCE(EXCLUDED."externalId","HubCreationAttempt"."externalId"),"updatedAt"=NOW()''',
            (key, reason, page_id))

    def hold(self, key, reason, *, page_id=None):
        with connect() as conn:
            self._hold(conn, key, reason, page_id)

    def clear(self, key):
        PostgresCreationJournal().dismiss_hold(key, 'notion_todo_review')
