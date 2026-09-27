"""関連追加の進捗と、変更前に確定した配送義務を別々に保持する。"""
from __future__ import annotations

import logging
import os
import psycopg
from psycopg.rows import dict_row

from src.sync_engine.record_sync_lock import acquire_record_sync_lock, RecordSyncBusy

logger = logging.getLogger(__name__)


class ProjectProductLinkQueue:
    def _connect(self):
        return psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row,
                              connect_timeout=10, options="-c statement_timeout=10000")

    def enqueue(self, project_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''INSERT INTO "ProjectProductLinkTask" ("projectId") VALUES (%s)
                ON CONFLICT ("projectId") DO UPDATE SET "updatedAt"=NOW()''', (project_id,))

    def get(self, project_id: str):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('SELECT * FROM "ProjectProductLinkTask" WHERE "projectId"=%s', (project_id,))
            return cur.fetchone()

    def plan(self, project_id: str, pairs: list[tuple[str, str]]) -> None:
        """未実行ペアだけ最新の案件に合わせる。配送義務には触れない。"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT "productId", "clientId" FROM "ProjectProductLinkPair"
                WHERE "projectId"=%s''', (project_id,))
            wanted = set(pairs)
            for row in cur.fetchall():
                pair = (row['productId'], row['clientId'])
                if pair not in wanted:
                    cur.execute('''DELETE FROM "ProjectProductLinkPair"
                        WHERE "projectId"=%s AND "productId"=%s AND "clientId"=%s''', (project_id, *pair))
            cur.executemany('''INSERT INTO "ProjectProductLinkPair" ("projectId", "productId", "clientId")
                VALUES (%s,%s,%s) ON CONFLICT DO NOTHING''', [(project_id, p, c) for p, c in pairs])
            self._hold_pairs_for_deliveries(cur, project_id)

    @staticmethod
    def _hold_pairs_for_deliveries(cur, project_id: str) -> None:
        # 配送保留中の対象を追加処理が繰り返し再試行しない。新しく計画されたペアにも適用。
        cur.execute('''UPDATE "ProjectProductLinkPair" p SET state='held',
            "lastError"=d."lastError", "errorDbKey"=d."dbKey", "errorNotionId"=d."notionId"
            FROM "ProjectProductLinkDelivery" d WHERE p."projectId"=%s
            AND d."projectId"=p."projectId" AND p.state='pending' AND d.state='held'
            AND ((d."dbKey"='product' AND p."productId"=d."notionId")
              OR (d."dbKey"='client_master' AND p."clientId"=d."notionId"))''', (project_id,))

    def pairs(self, project_id: str, limit: int):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT "productId", "clientId" FROM "ProjectProductLinkPair"
                WHERE "projectId"=%s AND state='pending' ORDER BY "productId", "clientId" LIMIT %s''',
                        (project_id, limit))
            return [(r['productId'], r['clientId']) for r in cur.fetchall()]

    def require_delivery(self, project_id: str, product_id: str, client_id: str) -> None:
        # PATCH前に双方の配送義務と含まれるべき相手IDを同じトランザクションで確定する。
        with self._connect() as conn, conn.cursor() as cur:
            cur.executemany('''INSERT INTO "ProjectProductLinkDelivery"
                ("projectId", "dbKey", "notionId", "expectedIds") VALUES (%s,%s,%s,%s)
                ON CONFLICT ("projectId", "dbKey", "notionId") DO UPDATE SET
                "expectedIds"=ARRAY(SELECT DISTINCT unnest("ProjectProductLinkDelivery"."expectedIds" || EXCLUDED."expectedIds"))''',
                [(project_id, 'product', product_id, [client_id]),
                 (project_id, 'client_master', client_id, [product_id])])

    def expected_ids(self, project_id: str, db_key: str, notion_id: str) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT "expectedIds" FROM "ProjectProductLinkDelivery"
                WHERE "projectId"=%s AND "dbKey"=%s AND "notionId"=%s''', (project_id, db_key, notion_id))
            row = cur.fetchone()
            if row is None:
                raise RuntimeError('配送義務が存在しません')
            return row['expectedIds']

    def unconfirmed(self, project_id: str, db_key: str, notion_id: str) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE "ProjectProductLinkDelivery" SET "verificationAttempts"="verificationAttempts"+1
                WHERE "projectId"=%s AND "dbKey"=%s AND "notionId"=%s RETURNING "verificationAttempts"''',
                (project_id, db_key, notion_id))
            return cur.fetchone()['verificationAttempts']

    def finish_pair(self, project_id: str, product_id: str, client_id: str) -> None:
        self._pair_state(project_id, product_id, client_id, 'done')

    def hold_pair(self, project_id, product_id, client_id, error, db_key, notion_id) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE "ProjectProductLinkPair" SET state='held', "lastError"=%s,
                "errorDbKey"=%s, "errorNotionId"=%s
                WHERE "projectId"=%s AND "productId"=%s AND "clientId"=%s''',
                        (error, db_key, notion_id, project_id, product_id, client_id))

    def _pair_state(self, project_id, product_id, client_id, state):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE "ProjectProductLinkPair" SET state=%s
                WHERE "projectId"=%s AND "productId"=%s AND "clientId"=%s''',
                        (state, project_id, product_id, client_id))

    def deliveries(self, project_id: str, limit: int):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT "dbKey", "notionId" FROM "ProjectProductLinkDelivery"
                WHERE "projectId"=%s AND state='pending' ORDER BY "dbKey", "notionId" LIMIT %s''',
                        (project_id, limit))
            return [(r['dbKey'], r['notionId']) for r in cur.fetchall()]

    def finish_delivery(self, project_id: str, db_key: str, notion_id: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''DELETE FROM "ProjectProductLinkDelivery"
                WHERE "projectId"=%s AND "dbKey"=%s AND "notionId"=%s''', (project_id, db_key, notion_id))

    def hold_delivery(self, project_id: str, db_key: str, notion_id: str, error: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE "ProjectProductLinkDelivery" SET state='held', "lastError"=%s
                WHERE "projectId"=%s AND "dbKey"=%s AND "notionId"=%s''', (error, project_id, db_key, notion_id))
            self._hold_pairs_for_deliveries(cur, project_id)

    def complete(self, project_id: str) -> bool:
        """進捗未完了・恒久保留・配送義務があればタスクを消さない。"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''DELETE FROM "ProjectProductLinkTask" t WHERE "projectId"=%s AND NOT t."evaluationHeld"
                AND NOT EXISTS (SELECT 1 FROM "ProjectProductLinkPair" p
                    WHERE p."projectId"=t."projectId" AND p.state<>'done')
                AND NOT EXISTS (SELECT 1 FROM "ProjectProductLinkDelivery" d
                    WHERE d."projectId"=t."projectId") RETURNING "projectId"''', (project_id,))
            return cur.fetchone() is not None

    def defer(self, project_id: str) -> None:
        """量・時間で区切った案件を少し後ろへ回し、他の案件を飢餓させない。"""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE "ProjectProductLinkTask" SET "nextAttemptAt"=NOW()+INTERVAL '1 minute',
                "updatedAt"=NOW() WHERE "projectId"=%s''', (project_id,))

    def fail(self, project_id: str, error: str, *, db_key: str = 'project', notion_id: str | None = None,
             permanent: bool = False, evaluation: bool = False) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''UPDATE "ProjectProductLinkTask" SET attempts=attempts+1,
                "lastError"=%s, "errorDbKey"=%s, "errorNotionId"=%s,
                "evaluationHeld"=("evaluationHeld" OR %s),
                "nextAttemptAt"=NOW() + make_interval(mins => LEAST(1440, 15 * (2 ^ LEAST(attempts, 7)))::integer),
                "updatedAt"=NOW() WHERE "projectId"=%s''',
                        (error, db_key, notion_id or project_id, permanent and evaluation, project_id))
            if permanent and evaluation:
                cur.execute('''UPDATE "ProjectProductLinkPair" SET state='held', "lastError"=%s,
                    "errorDbKey"=%s, "errorNotionId"=%s WHERE "projectId"=%s AND state='pending' ''',
                            (error, db_key, notion_id or project_id, project_id))

    def resume(self, project_id: str) -> None:
        """原因を修正後、案件ロック内で明示的に再開する。配送義務は捨てない。"""
        with self._connect() as conn, conn.cursor() as cur:
            for table in ('ProjectProductLinkPair', 'ProjectProductLinkDelivery'):
                cur.execute(f'''UPDATE "{table}" SET state='pending' WHERE "projectId"=%s AND state='held' ''',
                            (project_id,))
            cur.execute('UPDATE "ProjectProductLinkDelivery" SET "verificationAttempts"=0 WHERE "projectId"=%s', (project_id,))
            cur.execute('''UPDATE "ProjectProductLinkTask" SET "evaluationHeld"=false, attempts=0,
                "nextAttemptAt"=NOW(), "lastError"=NULL, "errorDbKey"=NULL, "errorNotionId"=NULL
                WHERE "projectId"=%s''', (project_id,))

    def pending(self, limit: int = 3) -> list[str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute('''SELECT t."projectId" FROM "ProjectProductLinkTask" t
                WHERE t."nextAttemptAt"<=NOW() AND (
                    NOT t."evaluationHeld" AND NOT EXISTS (
                        SELECT 1 FROM "ProjectProductLinkPair" p WHERE p."projectId"=t."projectId" AND p.state='held')
                    AND NOT EXISTS (SELECT 1 FROM "ProjectProductLinkDelivery" d
                        WHERE d."projectId"=t."projectId" AND d.state='held')
                    OR EXISTS (SELECT 1 FROM "ProjectProductLinkPair" p WHERE p."projectId"=t."projectId" AND p.state='pending')
                    OR EXISTS (SELECT 1 FROM "ProjectProductLinkDelivery" d WHERE d."projectId"=t."projectId" AND d.state='pending'))
                ORDER BY t."nextAttemptAt", t."projectId" LIMIT %s''', (limit,))
            return [r['projectId'] for r in cur.fetchall()]

    def contains(self, project_id: str) -> bool:
        return self.get(project_id) is not None


def drain_project_product_links(store, linker, *, limit: int = 3) -> dict[str, int]:
    queue = linker.queue
    result = {"completed": 0, "failed": 0, "busy": 0, "no_delivery_this_run": 0,
              "unsupported_relations": 0, "deferred": 0}
    for project_id in queue.pending(limit):
        try:
            with acquire_record_sync_lock(store, "project", project_id):
                if not queue.contains(project_id):
                    continue
                # 案件自体のmappingが消えても、既に発生した配送義務を消化する。
                from src.sync_engine.id_mapping import IdMapping
                mapping = store.get(project_id) or IdMapping(notion_key=project_id, db_key="project")
                try:
                    related = linker(mapping)
                except Exception as exc:
                    logger.warning("商品関連の再処理失敗: %s", type(exc).__name__)
                    result["failed"] += 1
                else:
                    if queue.contains(project_id):
                        result['deferred'] += 1
                    else:
                        result['completed'] += 1
                    result['no_delivery_this_run'] += int(not related)
                    result['unsupported_relations'] += sum(bool(p.skipped_tools) for p in related)
        except RecordSyncBusy:
            result['busy'] += 1
    return result
