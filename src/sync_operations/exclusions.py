"""Q052/Q053で確定した2件だけを除外する。元の外部レコードは残す。"""
from src.db_schema.base import Tool
from src.sync_operations.product_holds import connect, require_manager
from src.sync_review.domain import ReviewConflict

EXCLUSIONS = {
    '62388': {'reason': '不正状態のため同期対象外', 'decision': 'Q052'},
    '62202': {'reason': '取引先名が空のため同期対象外', 'decision': 'Q053'},
}


def is_excluded(tool, db_key, external_id):
    return tool in (Tool.KINTONE, 'kintone') and db_key == 'client_master' and str(external_id) in EXCLUSIONS


def list_exclusions(actor_id):
    with connect() as conn:
        require_manager(conn, actor_id)
        rows = conn.execute('''SELECT DISTINCT ON ("subjectId") "subjectId","actorId","createdAt"
            FROM "SyncOperationHistory" WHERE kind='record_exclusion_ack'
            ORDER BY "subjectId","createdAt" DESC''').fetchall()
    acknowledgements = {row['subjectId']: row for row in rows}
    return {'items': [dict(id=identifier, tool='kintone', dbKey='client_master', **value,
                          acknowledgement=acknowledgements.get(identifier)) for identifier, value in EXCLUSIONS.items()]}


def acknowledge_exclusion(actor_id, external_id):
    if external_id not in EXCLUSIONS:
        raise ReviewConflict('確認対象ではありません')
    import uuid
    from psycopg.types.json import Jsonb
    with connect() as conn:
        require_manager(conn, actor_id)
        conn.execute('''INSERT INTO "SyncOperationHistory"
            (id,"actorId",kind,"subjectId","beforeState","afterState")
            VALUES (%s,%s,'record_exclusion_ack',%s,%s,%s)''',
            (str(uuid.uuid4()), actor_id, external_id, Jsonb({}), Jsonb({'excluded': True, **EXCLUSIONS[external_id]})))
    return {'id': external_id, 'state': 'acknowledged'}
