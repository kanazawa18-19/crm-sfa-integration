"""統合済み旧IDの共通解決。機能有効時はDB障害を握りつぶさない。"""
import os
from src.sync_operations.product_holds import connect
from src.record_merge.domain import MergeHeld


def enabled():
    return os.environ.get('RECORD_MERGE_ENABLED') == 'true'


def history_available():
    """操作停止後も確定済み旧IDを守る。移行前の環境だけ従来経路に戻す。"""
    if enabled(): return True
    if not os.environ.get('DATABASE_URL'): return False
    with connect() as conn:
        row = conn.execute('SELECT to_regclass(%s) AS name', ('"RecordMergeAlias"',)).fetchone()
        return row['name'] is not None


def resolve_alias(db_key, tool, old_id):
    if not history_available():
        return None
    with connect() as conn:
        if db_key is None:
            rows = conn.execute('SELECT "dbKey","targetId" FROM "RecordMergeAlias" WHERE tool=%s AND "oldId"=%s',
                                (tool, str(old_id))).fetchall()
            if len(rows) > 1:
                raise MergeHeld('旧IDの対応が複数あります')
            row = rows[0] if rows else None
            if row is None: return None
            db_key, target = row['dbKey'], row['targetId']
        else:
            row = conn.execute('SELECT "targetId" FROM "RecordMergeAlias" WHERE "dbKey"=%s AND tool=%s AND "oldId"=%s',
                               (db_key, tool, str(old_id))).fetchone()
            if row is None: return None
            target = row['targetId']
        seen = {str(old_id)}
        for _ in range(20):
            if target in seen: raise MergeHeld('旧IDの対応が循環しています')
            seen.add(target)
            row = conn.execute('SELECT "targetId" FROM "RecordMergeAlias" WHERE "dbKey"=%s AND tool=\'notion\' AND "oldId"=%s',
                               (db_key, target)).fetchone()
            if row is None: return target
            target = row['targetId']
        raise MergeHeld('旧IDの対応が深すぎます')


def require_record_available(db_key, notion_id):
    if not history_available(): return
    with connect() as conn:
        row = conn.execute('''SELECT id FROM "RecordMergeJob" WHERE (("dbKey"=%s AND ("sourceId"=%s OR "targetId"=%s)) OR EXISTS (
                SELECT 1 FROM jsonb_array_elements(snapshot->'children') c
                WHERE c->>'dbKey'=%s AND c->>'id'=%s)) AND state IN ('approved','running','held') LIMIT 1''',
            (db_key, notion_id, notion_id, db_key, notion_id)).fetchone()
        if row:
            from src.sync_engine.record_sync_lock import RecordSyncBusy
            raise RecordSyncBusy('管理者が統合中です。処理再開後に同期を再送してください')


def hold_alias_event(event, mapping):
    """旧レコードの時刻だけで正本の最新編集を上書きしない。比較待ちを永続化する。"""
    if not history_available():
        raise MergeHeld('旧IDの変更を比較する機能が有効ではありません')
    import json
    import uuid
    from psycopg.types.json import Jsonb
    from src.record_merge.domain import digest
    properties = json.loads(json.dumps(event.properties, default=str))
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, digest({'db': event.db_key, 'tool': event.source_tool.value,
        'old': event.external_id, 'time': event.occurred_at.isoformat(), 'properties': properties})))
    with connect() as conn:
        conn.execute('''INSERT INTO "RecordMergeAliasEvent"
            (id,"dbKey","sourceTool","oldId","targetId","eventAt",properties)
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING''',
            (identifier, event.db_key, event.source_tool.value, event.external_id,
             mapping.notion_key, event.occurred_at, Jsonb(properties)))
    return identifier
