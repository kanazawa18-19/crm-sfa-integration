"""受理されたが完了していない同期を読取専用で観測する。"""
import hashlib
import json
from src.diagnostics.webhook_health import check, utc, stamp, TIMEOUT


def summarize(rows, now):
    rows = [row for row in rows if row.get('propertyName') != '__baseline__']
    # Q052の旧受理履歴は消さず、再配送を要する未完了とは分ける。
    excluded = [row for row in rows if row['dbKey'] == 'client_master'
                and row['notionKey'] == '3d8d8ea8-d4f3-817d-a1c8-f30516aacc8d'
                and row.get('propertyName') is None]
    rows = [row for row in rows if row not in excluded]
    field_count = sum(row.get('propertyName') is not None for row in rows)
    record_count = len({(row['dbKey'], row['notionKey']) for row in rows})
    details = dict(record_count=record_count, field_count=field_count,
                   count_unit='watermark_rows',
                   legacy_count=len(rows) - field_count, excluded_legacy_count=len(excluded),
                   exclusions_by_decision={'Q052': len(excluded)} if excluded else {})
    if not rows:
        return check('unfinished_sync', 'ok', '受理後に未完了の同期はありません', count=0, by_database={}, **details)
    ordered = sorted(rows, key=lambda row: (utc(row['acceptedAt']), row['dbKey'], row['notionKey'], row.get('propertyName') or ''))
    oldest = ordered[0]
    identity = [oldest['dbKey'], oldest['notionKey'], stamp(utc(oldest['acceptedAt']))]
    if oldest.get('propertyName') is not None:
        identity.append(oldest['propertyName'])
    counts = {}
    for row in rows: counts[row['dbKey']] = counts.get(row['dbKey'], 0) + 1
    # acceptedAtは外部イベント時刻。古いだけでは異常と断定せず同一対象の再観測へ。
    return check('unfinished_sync', 'pending', '受理後の完了待ちがあります。同一対象を再観測します',
        count=len(rows), by_database=counts, **details, oldest_event_at=identity[2], activity_at=now.isoformat(),
        fingerprint=hashlib.sha256(json.dumps(identity).encode()).hexdigest())


def read_unfinished(now):
    import os
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=TIMEOUT, row_factory=dict_row) as conn:
        conn.read_only = True
        conn.execute("SET LOCAL statement_timeout='5000ms'")
        rows = conn.execute('''SELECT "dbKey","notionKey","acceptedAt", NULL::text AS "propertyName"
            FROM "RecordSyncWatermark"
            WHERE "acceptedAt" IS NOT NULL AND ("completedAt" IS NULL OR "acceptedAt">"completedAt")
            UNION ALL
            SELECT "dbKey","notionKey","acceptedAt","propertyName" FROM "RecordSyncFieldWatermark"
            WHERE "propertyName" <> '__baseline__'
              AND ("completedAt" IS NULL OR "acceptedAt">"completedAt")''').fetchall()
    return summarize(rows, now)
