"""商品関連保留を、表示した差分と共通ロックを確認して再開する。"""
from __future__ import annotations

from contextlib import contextmanager
import os
import uuid

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.sync_engine.record_sync_lock import acquire_record_sync_lock
from src.sync_engine.won_product_link_queue import ProjectProductLinkQueue
from src.sync_review.domain import ReviewConflict, ReviewForbidden, snapshot_hash


@contextmanager
def connect():
    with psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row, connect_timeout=10) as conn:
        conn.execute("SET LOCAL statement_timeout = '10s'")
        yield conn


def require_manager(conn, actor_id):
    row = conn.execute('SELECT "isManager" FROM "User" WHERE id=%s', (actor_id,)).fetchone()
    if not row or not row['isManager']:
        raise ReviewForbidden('マネージャーだけが処理できます')


def read_product_hold(conn, project_id):
    row = conn.execute('''SELECT jsonb_build_object(
        'task',to_jsonb(t),
        'pairs',COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p."productId",p."clientId")
            FROM "ProjectProductLinkPair" p WHERE p."projectId"=t."projectId"),'[]'::jsonb),
        'deliveries',COALESCE((SELECT jsonb_agg(to_jsonb(d) ORDER BY d."dbKey",d."notionId")
            FROM "ProjectProductLinkDelivery" d WHERE d."projectId"=t."projectId"),'[]'::jsonb)
        ) AS snapshot FROM "ProjectProductLinkTask" t WHERE t."projectId"=%s''', (project_id,)).fetchone()
    return row['snapshot'] if row else None


def is_held(snapshot):
    return bool(snapshot['task']['evaluationHeld'] or any(
        row['state'] == 'held' for row in snapshot['pairs'] + snapshot['deliveries']))


def hold_snapshot_hash(snapshot):
    # 再試行時刻・回数だけの変化は、管理者が確認する保留内容を変えない。
    task = {key: value for key, value in snapshot['task'].items()
            if key not in {'attempts', 'nextAttemptAt', 'updatedAt'}}
    deliveries = [{key: value for key, value in row.items() if key != 'verificationAttempts'}
                  for row in snapshot['deliveries']]
    return snapshot_hash({'task': task, 'pairs': snapshot['pairs'], 'deliveries': deliveries})


def list_product_holds(actor_id, *, offset=0, limit=50):
    if not 0 <= offset <= 500000 or not 1 <= limit <= 50:
        raise ValueError('一覧の取得範囲が不正です')
    with connect() as conn:
        require_manager(conn, actor_id)
        rows = conn.execute('''SELECT t."projectId" FROM "ProjectProductLinkTask" t
            WHERE t."evaluationHeld" OR EXISTS (SELECT 1 FROM "ProjectProductLinkPair" p
                WHERE p."projectId"=t."projectId" AND p.state='held')
              OR EXISTS (SELECT 1 FROM "ProjectProductLinkDelivery" d
                WHERE d."projectId"=t."projectId" AND d.state='held')
            ORDER BY t."updatedAt",t."projectId" OFFSET %s LIMIT %s''', (offset, limit + 1)).fetchall()
        result = []
        for row in rows[:limit]:
            snapshot = read_product_hold(conn, row['projectId'])
            if snapshot is None or not is_held(snapshot):
                continue
            result.append({'projectId': row['projectId'], 'snapshot': snapshot, 'hash': hold_snapshot_hash(snapshot)})
        return {'items': result, 'hasMore': len(rows) > limit}


def resume_product_hold(*, actor_id, project_id, expected_hash, store):
    with acquire_record_sync_lock(store, 'project', project_id):
        with connect() as conn:
            require_manager(conn, actor_id)
            mapping = store.get(project_id)
            if mapping is None or mapping.db_key != 'project':
                raise ReviewConflict('案件の同期先を確認できません')
            before = read_product_hold(conn, project_id)
            if before is None or not is_held(before):
                raise ReviewConflict('この案件は保留中ではありません')
            if hold_snapshot_hash(before) != expected_hash:
                raise ReviewConflict('表示後に保留内容が変わりました。読み直してください')
            with conn.cursor() as cur:
                ProjectProductLinkQueue.resume_with_cursor(cur, project_id)
            after = read_product_hold(conn, project_id)
            conn.execute('''INSERT INTO "SyncOperationHistory"
                (id,"actorId",kind,"subjectId","beforeState","afterState") VALUES (%s,%s,%s,%s,%s,%s)''',
                (str(uuid.uuid4()), actor_id, 'product_link_resume', project_id, Jsonb(before), Jsonb(after)))
            return {'projectId': project_id, 'state': 'pending'}
