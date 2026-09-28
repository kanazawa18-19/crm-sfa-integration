"""空欄削除の承認と配送進捗をPostgresへ保存する。"""
from __future__ import annotations

from contextlib import contextmanager
import os
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.sync_review.domain import decide, snapshot_hash, ReviewConflict, ReviewForbidden


class FieldReviewJournal:
    @contextmanager
    def _connect(self):
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=10) as conn:
            conn.execute("SET LOCAL statement_timeout = '10s'")
            yield conn

    def source_values(self, db_key, notion_key, source_tool):
        with self._connect() as conn:
            row = conn.execute('SELECT "values" FROM "SyncSourceObservation" WHERE "dbKey"=%s AND "notionKey"=%s AND "sourceTool"=%s',
                               (db_key, notion_key, source_tool)).fetchone()
            return row['values'] if row else {}

    def observe_source(self, db_key, notion_key, source_tool, values, event_at):
        # 呼出元の共通レコードロック内で、起票成功後に観測値を進める。
        with self._connect() as conn:
            conn.execute('''INSERT INTO "SyncSourceObservation" ("dbKey","notionKey","sourceTool","values","eventAt")
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT ("dbKey","notionKey","sourceTool")
                DO UPDATE SET "values"="SyncSourceObservation"."values" || EXCLUDED."values","eventAt"=EXCLUDED."eventAt"
                WHERE "SyncSourceObservation"."eventAt" <= EXCLUDED."eventAt"''',
                (db_key, notion_key, source_tool, Jsonb(values), event_at))

    def enqueue(self, *, db_key, notion_key, property_name, source_tool, event_at, snapshot):
        with self._connect() as conn:
            row = conn.execute('''INSERT INTO "SyncFieldReview"
                (id,"dbKey","notionKey","propertyName","sourceTool","eventAt",snapshot,"snapshotHash")
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT ("dbKey","notionKey","propertyName","sourceTool","eventAt")
                DO UPDATE SET "updatedAt"="SyncFieldReview"."updatedAt" RETURNING *''',
                (str(uuid.uuid4()), db_key, notion_key, property_name, source_tool, event_at,
                 Jsonb(snapshot), snapshot_hash(snapshot))).fetchone()
            return row

    def get(self, review_id):
        with self._connect() as conn:
            return conn.execute('SELECT * FROM "SyncFieldReview" WHERE id=%s', (review_id,)).fetchone()

    def active(self, db_key, notion_key, property_name):
        with self._connect() as conn:
            return conn.execute('''SELECT * FROM "SyncFieldReview"
                WHERE "dbKey"=%s AND "notionKey"=%s AND "propertyName"=%s
                AND state NOT IN ('done','superseded') ORDER BY "createdAt" DESC LIMIT 1''',
                (db_key, notion_key, property_name)).fetchone()

    def active_for_record(self, db_key, notion_key):
        with self._connect() as conn:
            rows = conn.execute('''SELECT * FROM "SyncFieldReview"
                WHERE "dbKey"=%s AND "notionKey"=%s
                AND state NOT IN ('done','superseded') ORDER BY "createdAt"''',
                (db_key, notion_key)).fetchall()
            return {row['propertyName']: row for row in rows}

    def decision(self, review_id, *, actor_id, action, revision, restore_from=None, new_snapshot=None):
        """権限をDBで再取得する。クライアントのisManager/メール申告は使わない。"""
        with self._connect() as conn:
            user = conn.execute('SELECT "isManager" FROM "User" WHERE id=%s', (actor_id,)).fetchone()
            if not user or not user['isManager']:
                raise ReviewForbidden("マネージャーだけが処理できます")
            row = conn.execute('SELECT * FROM "SyncFieldReview" WHERE id=%s FOR UPDATE', (review_id,)).fetchone()
            if row is None:
                raise ReviewConflict("対象がありません")
            result = decide(state=row['state'], action=action, is_manager=True,
                            expected_revision=revision, actual_revision=row['revision'])
            following = result.state
            if action == 'recheck':
                if new_snapshot is None:
                    raise ReviewConflict('再確認の現在値がありません')
                from src.sync_review.domain import is_blank
                source = new_snapshot.get(row['sourceTool'])
                if source and source['supported']:
                    logical = source.get('canonical', source.get('value')) if row['propertyName'] == 'メモ' else source.get('value')
                    if not is_blank(logical):
                        following = 'superseded'
                conn.execute('''UPDATE "SyncFieldReview" SET snapshot=%s,"snapshotHash"=%s,
                    progress='{}',"lastError"=NULL WHERE id=%s''',
                    (Jsonb(new_snapshot), snapshot_hash(new_snapshot), review_id))
            if action == 'restore':
                item = row['snapshot'].get(restore_from)
                from src.sync_review.domain import is_blank
                if not item or 'canonical' not in item or is_blank(item['canonical']):
                    raise ReviewConflict("復元できる元値を選択してください")
                conn.execute('''UPDATE "SyncFieldReview" SET progress=progress || %s WHERE id=%s''',
                             (Jsonb({'_restore': {'value': item['canonical']}}), review_id))
            updated = conn.execute('''UPDATE "SyncFieldReview" SET state=%s,revision=revision+1,
                "updatedAt"=NOW() WHERE id=%s RETURNING *''', (following, review_id)).fetchone()
            conn.execute('''INSERT INTO "SyncFieldReviewHistory" ("reviewId","actorId",action,revision,snapshot)
                VALUES (%s,%s,%s,%s,%s)''', (review_id, actor_id, action, updated['revision'], Jsonb(row['snapshot'])))
            return updated

    def record_progress(self, review_id, tool, value):
        with self._connect() as conn:
            conn.execute('''UPDATE "SyncFieldReview" SET progress=progress || %s,
                "updatedAt"=NOW() WHERE id=%s''', (Jsonb({tool: value}), review_id))

    def finish(self, review_id, state, *, error=None):
        if state not in {'done', 'failed', 'superseded'}:
            raise ValueError('実行結果の状態が不正です')
        with self._connect() as conn:
            conn.execute('''UPDATE "SyncFieldReview" SET state=%s,"lastError"=%s,
                revision=revision+1,"updatedAt"=NOW() WHERE id=%s''', (state, error, review_id))
