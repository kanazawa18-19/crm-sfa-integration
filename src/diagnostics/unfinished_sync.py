"""受理されたが完了していない同期を読取専用で観測する。"""
import hashlib
import json
from datetime import timezone
from src.diagnostics.webhook_health import check, utc, stamp, TIMEOUT


def summarize(rows, now):
    if not rows:
        return check('unfinished_sync', 'ok', '受理後に未完了の同期はありません', count=0, by_database={})
    ordered = sorted(rows, key=lambda row: (utc(row['acceptedAt']), row['dbKey'], row['notionKey']))
    oldest = ordered[0]
    identity = [oldest['dbKey'], oldest['notionKey'], stamp(utc(oldest['acceptedAt']))]
    counts = {}
    for row in rows: counts[row['dbKey']] = counts.get(row['dbKey'], 0) + 1
    # acceptedAtは外部イベント時刻。古いだけでは異常と断定せず同一対象の再観測へ。
    return check('unfinished_sync', 'pending', '受理後の完了待ちがあります。同一対象を再観測します',
        count=len(rows), by_database=counts, oldest_event_at=identity[2], activity_at=now.isoformat(),
        fingerprint=hashlib.sha256(json.dumps(identity).encode()).hexdigest())


def read_unfinished(now):
    import os
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(os.environ['DATABASE_URL'], connect_timeout=TIMEOUT, row_factory=dict_row) as conn:
        conn.read_only = True
        conn.execute("SET LOCAL statement_timeout='5000ms'")
        rows = conn.execute('''SELECT "dbKey","notionKey","acceptedAt" FROM "RecordSyncWatermark"
            WHERE "acceptedAt" IS NOT NULL AND ("completedAt" IS NULL OR "acceptedAt">"completedAt")''').fetchall()
    return summarize(rows, now)
