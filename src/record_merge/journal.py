"""統合手順と旧IDを永続化し、すべての判断を共通操作履歴へ残す。"""
import uuid
from psycopg.types.json import Jsonb
from src.record_merge.domain import MergeHeld
from src.record_merge.application import validate_plan
from src.sync_operations.product_holds import connect, require_manager


def history(conn, actor_id, operation_id, before, after):
    conn.execute('''INSERT INTO "SyncOperationHistory"
        (id,"actorId",kind,"subjectId","beforeState","afterState")
        VALUES (%s,%s,'record_merge',%s,%s,%s)''',
        (str(uuid.uuid4()), actor_id, operation_id, Jsonb(before), Jsonb(after)))


class MergeJournal:
    def get_authorized(self, operation_id, actor_id):
        with connect() as conn:
            require_manager(conn, actor_id)
            row = conn.execute('SELECT * FROM "RecordMergeJob" WHERE id=%s', (operation_id,)).fetchone()
            if row is None:
                raise MergeHeld('統合対象がありません')
            return row

    def create(self, snapshot, steps, actor_id):
        plan_hash = validate_plan(snapshot, steps)
        operation_id = str(uuid.uuid4())
        with connect() as conn:
            require_manager(conn, actor_id)
            conn.execute('''INSERT INTO "RecordMergeJob"
                (id,"dbKey","sourceId","targetId",snapshot,steps,"planHash","actorId")
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''', (operation_id, snapshot['dbKey'],
                snapshot['sourceId'], snapshot['targetId'], Jsonb(snapshot), Jsonb(steps), plan_hash, actor_id))
            history(conn, actor_id, operation_id, {}, {'state': 'draft', 'planHash': plan_hash})
        return self.get_authorized(operation_id, actor_id)

    def decide(self, operation_id, actor_id, expected_hash, action):
        with connect() as conn:
            require_manager(conn, actor_id)
            row = conn.execute('SELECT * FROM "RecordMergeJob" WHERE id=%s FOR UPDATE', (operation_id,)).fetchone()
            if row is None or row['planHash'] != expected_hash or row['state'] != 'draft':
                raise MergeHeld('比較内容が変更されています。読み直してください')
            state = {'approve': 'approved', 'dismiss': 'dismissed'}.get(action)
            if state is None:
                raise MergeHeld('操作が不正です')
            conn.execute('UPDATE "RecordMergeJob" SET state=%s,"actorId"=%s,"updatedAt"=now() WHERE id=%s',
                         (state, actor_id, operation_id))
            history(conn, actor_id, operation_id, {'state': row['state']}, {'state': state})
        return self.get_authorized(operation_id, actor_id)

    def step_state(self, operation_id, index):
        with connect() as conn:
            row = conn.execute('SELECT progress FROM "RecordMergeJob" WHERE id=%s', (operation_id,)).fetchone()
            return row['progress'].get(str(index), {}).get('state') if row else None

    def _step(self, operation_id, index, actor_id, state, receipt=None):
        with connect() as conn:
            require_manager(conn, actor_id)
            row = conn.execute('SELECT * FROM "RecordMergeJob" WHERE id=%s FOR UPDATE', (operation_id,)).fetchone()
            if row is None or row['state'] not in {'approved','running','held'}:
                raise MergeHeld('統合の承認状態が変わりました')
            progress = dict(row['progress'])
            before = progress.get(str(index), {})
            progress[str(index)] = {'state': state, 'receipt': receipt}
            conn.execute('UPDATE "RecordMergeJob" SET progress=%s,state=\'running\',error=NULL,"updatedAt"=now() WHERE id=%s',
                         (Jsonb(progress), operation_id))
            history(conn, actor_id, operation_id, {'step': index, **before}, {'step': index, **progress[str(index)]})

    def reserve_step(self, operation_id, index, actor_id):
        self._step(operation_id, index, actor_id, 'reserved')

    def complete_step(self, operation_id, index, actor_id, receipt):
        self._step(operation_id, index, actor_id, 'done', receipt)

    def hold(self, operation_id, actor_id, reason):
        with connect() as conn:
            require_manager(conn, actor_id)
            conn.execute('UPDATE "RecordMergeJob" SET state=\'held\',error=%s,"updatedAt"=now() WHERE id=%s AND state<>\'done\'',
                         (reason[:1000], operation_id))
            history(conn, actor_id, operation_id, {}, {'state': 'held', 'reason': reason[:1000]})

    def finish_with_aliases(self, operation_id, actor_id):
        with connect() as conn:
            require_manager(conn, actor_id)
            job = conn.execute('SELECT * FROM "RecordMergeJob" WHERE id=%s FOR UPDATE', (operation_id,)).fetchone()
            if any(job['progress'].get(str(index), {}).get('state') != 'done' for index in range(len(job['steps']))):
                raise MergeHeld('未完了の統合手順があります')
            # 外部レコードは保持し、旧IDのみ確定した統合先へ向ける。
            for tool, old_id in job['snapshot']['aliases'].items():
                if old_id is None:
                    continue
                conn.execute('''INSERT INTO "RecordMergeAlias" ("dbKey",tool,"oldId","targetId","operationId")
                    VALUES (%s,%s,%s,%s,%s)''',
                    (job['dbKey'], tool, str(old_id), job['targetId'], operation_id))
            conn.execute('UPDATE "RecordMergeJob" SET state=\'done\',error=NULL,"updatedAt"=now() WHERE id=%s', (operation_id,))
            history(conn, actor_id, operation_id, {'state': job['state']}, {'state': 'done'})
