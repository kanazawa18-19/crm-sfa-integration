"""案件検索とSlack操作台帳のPostgres実装。"""
from __future__ import annotations

import os
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json


def _connect() -> psycopg.Connection[dict[str, Any]]:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise ValueError("DATABASE_URL is not set")
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=1,
                           options="-c timezone=UTC -c statement_timeout=1000")


def search_projects(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    text = query.strip()
    if len(text) < 2 or len(text) > 80:
        return []
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT p."notionPageId" AS id, p.data,
                              (SELECT string_agg(c."rawName", '／' ORDER BY c."rawName")
                               FROM "ClientNameIndex" c
                               WHERE c."notionPageId" IN
                                 (SELECT jsonb_array_elements_text(coalesce(p.data->'取引先マスター', '[]'::jsonb)))) AS client_name
                       FROM "ProjectMirror" p
                       WHERE p.data->>'案件名' ILIKE %s ESCAPE '\\'
                          OR EXISTS (SELECT 1 FROM "ClientNameIndex" c
                                     WHERE c."rawName" ILIKE %s ESCAPE '\\'
                                       AND c."notionPageId" IN
                                         (SELECT jsonb_array_elements_text(coalesce(p.data->'取引先マスター', '[]'::jsonb))))
                       ORDER BY p."lastEditedAt" DESC NULLS LAST LIMIT %s''',
                    (f"%{escaped}%", f"%{escaped}%", limit))
        return [{**row["data"], "id": row["id"], "client_name": row["client_name"]} for row in cur.fetchall()]


def get_project(page_id: str) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT p.data,
                              (SELECT string_agg(c."rawName", '／' ORDER BY c."rawName")
                               FROM "ClientNameIndex" c
                               WHERE c."notionPageId" IN
                                 (SELECT jsonb_array_elements_text(coalesce(p.data->'取引先マスター', '[]'::jsonb)))) AS client_name
                       FROM "ProjectMirror" p WHERE p."notionPageId"=%s''', (page_id,))
        row = cur.fetchone()
        return {**row["data"], "id": page_id, "client_name": row["client_name"]} if row else None


def recent_projects(actor_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''SELECT p."notionPageId" AS id, p.data,
                              (SELECT string_agg(c."rawName", '／' ORDER BY c."rawName")
                               FROM "ClientNameIndex" c
                               WHERE c."notionPageId" IN
                                 (SELECT jsonb_array_elements_text(coalesce(p.data->'取引先マスター', '[]'::jsonb)))) AS client_name
                       FROM "SlackCrmOperation" o JOIN "ProjectMirror" p
                         ON p."notionPageId"=o."targetId"
                       WHERE o."actorId"=%s AND o.kind='project_update' AND o.state='done'
                       ORDER BY o."createdAt" DESC LIMIT 20''', (actor_id,))
        result = []
        seen = set()
        for row in cur.fetchall():
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            result.append({**row["data"], "id": row["id"], "client_name": row["client_name"]})
            if len(result) >= limit:
                break
        return result


def enqueue(*, operation_id: str, actor_id: str, kind: str, target_id: str,
            expected: dict[str, Any], changes: dict[str, Any]) -> bool:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''INSERT INTO "SlackCrmOperation"
                       (id, "actorId", kind, "targetId", expected, changes)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING''',
                    (operation_id, actor_id, kind, target_id, Json(expected), Json(changes)))
        inserted = cur.rowcount == 1
        conn.commit()
        return inserted


def claim(operation_id: str | None = None) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE "SlackCrmOperation" SET state='processing', "startedAt"=now(),
                           "attempts"="attempts"+1
                       WHERE id=(SELECT id FROM "SlackCrmOperation"
                                 WHERE state='queued' AND ("processAfter" IS NULL OR "processAfter"<=now())
                                   AND (%s::text IS NULL OR id=%s)
                                 ORDER BY "createdAt" FOR UPDATE SKIP LOCKED LIMIT 1)
                       RETURNING *''', (operation_id, operation_id))
        row = cur.fetchone()
        conn.commit()
        return row


def finish(operation_id: str, *, state: str, result_page_id: str | None = None,
           error_code: str | None = None) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE "SlackCrmOperation"
                       SET state=%s, "resultPageId"=%s, "errorCode"=%s, "finishedAt"=now(),
                           "syncState"=CASE WHEN %s='done' THEN 'queued' ELSE NULL END
                       WHERE id=%s AND state='processing' ''',
                    (state, result_page_id, error_code, state, operation_id))
        conn.commit()


def claim_sync(operation_id: str | None = None) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE "SlackCrmOperation" SET "syncState"='processing',
                           "syncClaimedAt"=now(), "syncAttempts"="syncAttempts"+1
                       WHERE id=(SELECT id FROM "SlackCrmOperation"
                                 WHERE state='done' AND "syncState"='queued'
                                   AND ("syncAfter" IS NULL OR "syncAfter"<=now())
                                   AND (%s::text IS NULL OR id=%s)
                                 ORDER BY "createdAt" FOR UPDATE SKIP LOCKED LIMIT 1)
                       RETURNING *''', (operation_id, operation_id))
        row = cur.fetchone()
        conn.commit()
        return row


def finish_sync(operation_id: str, *, state: str, error: str | None = None) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE "SlackCrmOperation" SET "syncState"=%s, "syncError"=%s,
                           "syncClaimedAt"=NULL,
                           "syncAfter"=CASE WHEN %s='queued' THEN now()+interval '5 minutes' ELSE NULL END
                       WHERE id=%s AND "syncState"='processing' ''', (state, error, state, operation_id))
        conn.commit()


def requeue(operation_id: str) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE "SlackCrmOperation" SET state='queued', "startedAt"=NULL,
                           "processAfter"=now()+interval '30 seconds'
                       WHERE id=%s AND state='processing' ''', (operation_id,))
        conn.commit()


def claim_notification(operation_id: str | None = None) -> dict[str, Any] | None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE "SlackCrmOperation" SET "notifyClaimedAt"=now(),
                           "notifyAttempts"="notifyAttempts"+1
                       WHERE id=(SELECT id FROM "SlackCrmOperation"
                                 WHERE (state IN ('failed','conflict','unknown')
                                    OR (state='done' AND "syncState" IN ('done','partial','unmapped','failed')))
                                   AND "notifiedAt" IS NULL
                                   AND ("notifyAfter" IS NULL OR "notifyAfter"<=now())
                                   AND "notifyAttempts"<12
                                   AND ("notifyClaimedAt" IS NULL OR "notifyClaimedAt"<now()-interval '10 minutes')
                                   AND (%s::text IS NULL OR id=%s)
                                 ORDER BY "createdAt" FOR UPDATE SKIP LOCKED LIMIT 1)
                       RETURNING *''', (operation_id, operation_id))
        row = cur.fetchone()
        conn.commit()
        return row


def finish_notification(operation_id: str, *, success: bool) -> None:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE "SlackCrmOperation"
                       SET "notifyClaimedAt"=NULL,
                           "notifiedAt"=CASE WHEN %s THEN now() ELSE "notifiedAt" END,
                           "notifyAfter"=CASE WHEN %s THEN NULL ELSE now()+interval '5 minutes' END
                       WHERE id=%s''', (success, success, operation_id))
        conn.commit()


def recover_stale() -> int:
    """作成中の応答不明は再送せず保留。更新のみ再取得からやり直す。"""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute('''UPDATE "SlackCrmOperation"
                       SET state=CASE WHEN kind='action_create' THEN 'unknown' ELSE 'queued' END,
                           "errorCode"='worker_interrupted'
                       WHERE state='processing' AND "startedAt" < now() - interval '10 minutes' ''')
        count = cur.rowcount
        cur.execute('''UPDATE "SlackCrmOperation" SET "syncState"='queued', "syncClaimedAt"=NULL
                       WHERE "syncState"='processing' AND "syncClaimedAt"<now()-interval '10 minutes' ''')
        count += cur.rowcount
        conn.commit()
        return count
