"""新規登録で見つかった外部候補を、現在値に結び付けて管理者へ渡す。"""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid5, NAMESPACE_URL
from psycopg.types.json import Jsonb
from src.hub_creation.domain import CreationHeld
from src.record_merge.aliases import enabled
from src.record_merge.domain import digest
from src.sync_operations.product_holds import connect

_context = ContextVar('creation_candidate', default=None)


@contextmanager
def candidate_context(db_key, source_id, source_key, properties):
    token = _context.set({'dbKey': db_key, 'sourceId': source_id, 'sourceKey': source_key,
                          'sourceProperties': properties})
    try:
        yield
    finally:
        _context.reset(token)


def hold_duplicate(tool, db_key, external_id, record, reason, *, fetch=None):
    """見送りは同じ入力・同じ外部候補に限る。変更されれば再確認する。"""
    context = _context.get()
    if not enabled() or context is None:
        raise CreationHeld(reason)
    if fetch is not None:
        record = fetch()
    if not isinstance(record, dict) or not record:
        raise CreationHeld('候補の全項目を取得できません')
    snapshot = {**context, 'tool': tool, 'externalId': str(external_id), 'externalRecord': record}
    identifier = str(uuid5(NAMESPACE_URL, digest([db_key, tool, context['sourceId'], str(external_id)])))
    snapshot_hash = digest(snapshot)
    with connect() as conn:
        row = conn.execute('SELECT * FROM "RecordCreationCandidate" WHERE id=%s FOR UPDATE', (identifier,)).fetchone()
        if row and row['state'] in {'reserved', 'imported'}:
            raise CreationHeld('既存候補の取り込み開始済みです。管理画面で結果と統合を確認してください')
        if row and row['state'] == 'dismissed' and row['snapshotHash'] == snapshot_hash:
            return
        conn.execute('''INSERT INTO "RecordCreationCandidate" (id,"dbKey","sourceId",tool,"externalId",snapshot,"snapshotHash")
            VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO UPDATE SET
            snapshot=EXCLUDED.snapshot,"snapshotHash"=EXCLUDED."snapshotHash",
            state=CASE WHEN "RecordCreationCandidate"."snapshotHash"=EXCLUDED."snapshotHash" THEN "RecordCreationCandidate".state ELSE 'pending' END,
            "updatedAt"=now()''', (identifier, db_key, context['sourceId'], tool, str(external_id), Jsonb(snapshot), snapshot_hash))
    raise CreationHeld(reason + '（管理画面の外部登録候補に保存済み）')


class CreationCandidates:
    def __init__(self, operations):
        self.operations = operations
        self.store = operations.store

    def compare(self, actor_id, identifier):
        from src.db_schema.base import Tool
        from src.record_merge.domain import MergeHeld
        self.operations.authorize(actor_id)
        with connect() as conn:
            row = conn.execute('SELECT * FROM "RecordCreationCandidate" WHERE id=%s', (identifier,)).fetchone()
        if row is None:
            raise MergeHeld('外部候補がありません')
        target = self.operations.dispatcher._targets.get(Tool(row['tool']))
        external = target.get_record(row['externalId'], db_key=row['dbKey']) if target else None
        if not isinstance(external, dict) or not external:
            raise MergeHeld('外部候補の現在値を取得できません')
        page = self.operations.gateway.page(row['dbKey'], row['sourceId'])
        if page.get('archived') or page.get('in_trash'):
            raise MergeHeld('登録元はアーカイブされています。統合履歴を確認してください')
        mapping = self.store.find_by_external_id(Tool(row['tool']), row['externalId'], db_key=row['dbKey'])
        snapshot = {'id': identifier, 'dbKey': row['dbKey'], 'sourceId': row['sourceId'],
                    'tool': row['tool'], 'externalId': row['externalId'], 'external': external,
                    'notion': page['properties'], 'mappedId': mapping.notion_key if mapping else None,
                    'candidateHash': row['snapshotHash'], 'state': row['state']}
        from src.sync_engine.outbound_field_mapping import zoho_outbound_field_names, kintone_outbound_field_names
        from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value, PARSEABLE_NOTION_PROPERTY_TYPES
        table = (zoho_outbound_field_names() if row['tool'] == 'zoho' else kintone_outbound_field_names()).get(row['dbKey'], {})
        display = []
        for name, code in table.items():
            if name not in page['properties'] and code not in external: continue
            prop = page['properties'].get(name, {})
            value = parse_notion_property_value(prop) if prop.get('type') in PARSEABLE_NOTION_PROPERTY_TYPES else None
            display.append({'name': name, 'notion': value, 'external': external.get(code)})
        return {'snapshot': snapshot, 'hash': digest(snapshot), 'display': display}

    def decide(self, actor_id, identifier, expected_hash, action):
        from contextlib import ExitStack
        from src.db_schema.base import Tool
        from src.record_merge.domain import MergeHeld
        from src.record_merge.journal import history
        from src.sync_operations.product_holds import require_manager
        from src.sync_engine.record_sync_lock import acquire_record_sync_lock
        from src.sync_engine.id_mapping import IdMapping
        from src.sync_engine.new_record_builder import build_notion_properties_for_new_record
        from src.hub_creation.domain import require_notion_fields
        initial = self.compare(actor_id, identifier)
        snapshot = initial['snapshot']
        keys = {(snapshot['dbKey'], snapshot['sourceId']),
                (snapshot['dbKey'], 'external:' + snapshot['tool'] + ':' + snapshot['externalId'])}
        with ExitStack() as stack:
            for db_key, key in sorted(keys):
                stack.enter_context(acquire_record_sync_lock(self.store, db_key, key))
            current = self.compare(actor_id, identifier)
            if current['hash'] != expected_hash:
                raise MergeHeld('候補の内容が変更されています。比較し直してください')
            with connect() as conn:
                require_manager(conn, actor_id)
                row = conn.execute('SELECT * FROM "RecordCreationCandidate" WHERE id=%s FOR UPDATE', (identifier,)).fetchone()
                if action == 'dismiss_creation':
                    if row['state'] in {'reserved', 'imported'}:
                        raise MergeHeld('取り込み開始済みの候補は見送れません')
                    # 作成時の指紋を変えずに残す。次回入力・外部値が変われば再確認になる。
                    if row['snapshot']['externalRecord'] != snapshot['external']:
                        raise MergeHeld('検出後に候補が変わりました。元ページを再通知して候補を更新してください')
                    conn.execute('UPDATE "RecordCreationCandidate" SET state=\'dismissed\',"updatedAt"=now() WHERE id=%s', (identifier,))
                    history(conn, actor_id, identifier, snapshot, {'state': 'creation_dismissed'})
                    return {'state': 'dismissed'}
            if snapshot['mappedId']:
                return {'state': 'mapped', 'targetId': snapshot['mappedId'], 'sourceId': snapshot['sourceId'], 'dbKey': snapshot['dbKey']}
            imported = row['importedNotionId']
            if row['state'] == 'reserved' and not imported:
                raise MergeHeld('Notion取り込みの結果が不明です。自動で再作成せず実物を確認してください')
            if not imported:
                properties = build_notion_properties_for_new_record(source_tool=Tool(snapshot['tool']),
                    db_key=snapshot['dbKey'], external_id=snapshot['externalId'], raw_record=snapshot['external'], id_mapping_store=self.store)
                require_notion_fields(snapshot['dbKey'], properties)
                with connect() as conn:
                    require_manager(conn, actor_id)
                    conn.execute('UPDATE "RecordCreationCandidate" SET state=\'reserved\',snapshot=snapshot || %s,"updatedAt"=now() WHERE id=%s',
                        (Jsonb({'importProperties': properties, 'importExternal': snapshot['external']}), identifier))
                    history(conn, actor_id, identifier, snapshot, {'state': 'creation_import_reserved', 'properties': properties})
                marker = import_marker(identifier, snapshot['tool'], snapshot['dbKey'], snapshot['externalId'])
                imported = self.operations.gateway.clients[snapshot['dbKey']].create_page_once(properties, children=[
                    {'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [
                        {'type': 'text', 'text': {'content': marker}}]}}])
                with connect() as conn:
                    conn.execute('UPDATE "RecordCreationCandidate" SET "importedNotionId"=%s,"updatedAt"=now() WHERE id=%s', (imported, identifier))
            kwargs = {'zoho_id' if snapshot['tool'] == 'zoho' else 'kintone_id': snapshot['externalId']}
            self.store.upsert(IdMapping(imported, snapshot['dbKey'], **kwargs), expected_last_synced_at=None)
            with connect() as conn:
                conn.execute('UPDATE "RecordCreationCandidate" SET state=\'imported\',"updatedAt"=now() WHERE id=%s', (identifier,))
                history(conn, actor_id, identifier, snapshot, {'state': 'creation_imported', 'targetId': imported})
            return {'state': 'imported', 'targetId': imported, 'sourceId': snapshot['sourceId'], 'dbKey': snapshot['dbKey']}


def import_marker(identifier, tool, db_key, external_id):
    return f'既存外部レコードの取り込み: {tool}/{db_key}/{external_id}（確認番号 {identifier}）'


def reset_import_preview(operations, actor_id, identifier):
    """未作成の断定はしない。管理者が実物確認する対象を固定する。"""
    from src.record_merge.domain import MergeHeld
    current = CreationCandidates(operations).compare(actor_id, identifier)
    with connect() as conn:
        row = conn.execute('SELECT * FROM "RecordCreationCandidate" WHERE id=%s', (identifier,)).fetchone()
    if row is None or row['state'] != 'reserved' or row['importedNotionId'] or current['snapshot']['mappedId']:
        raise MergeHeld('未確定の取り込み予約ではありません')
    snapshot = {**current['snapshot'], 'updatedAt': row['updatedAt'].isoformat(),
                'marker': import_marker(identifier, row['tool'], row['dbKey'], row['externalId'])}
    return {'snapshot': snapshot, 'hash': digest(snapshot)}


def reset_import(operations, actor_id, identifier, expected_hash, confirmation):
    """本人が未作成を確認した予約だけ解除する。この操作では再作成しない。"""
    from src.record_merge.domain import MergeHeld
    from src.record_merge.journal import history
    from src.sync_engine.record_sync_lock import acquire_record_sync_locks
    from src.sync_operations.product_holds import require_manager
    if confirmation != 'Notionとごみ箱で未作成を確認しました':
        raise MergeHeld('Notionとごみ箱で確認番号のページがないことを確認してください')
    initial = reset_import_preview(operations, actor_id, identifier)['snapshot']
    keys = {(initial['dbKey'], initial['sourceId']),
            (initial['dbKey'], 'external:' + initial['tool'] + ':' + initial['externalId'])}
    with acquire_record_sync_locks(operations.store, keys):
        current = reset_import_preview(operations, actor_id, identifier)
        if current['hash'] != expected_hash:
            raise MergeHeld('未作成の確認後に内容が変わりました')
        with connect() as conn:
            require_manager(conn, actor_id)
            changed = conn.execute('''UPDATE "RecordCreationCandidate" SET state='pending',"updatedAt"=now()
                WHERE id=%s AND state='reserved' AND "importedNotionId" IS NULL RETURNING id''', (identifier,)).fetchone()
            if not changed:
                raise MergeHeld('予約の状態が変わりました')
            history(conn, actor_id, identifier, current['snapshot'],
                    {'state': 'creation_reservation_reset', 'confirmation': confirmation})
    return {'state': 'pending'}


def recovery_preview(operations, actor_id, identifier, page_id):
    from src.db_schema.base import Tool
    from src.record_merge.domain import MergeHeld
    from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value
    operations.authorize(actor_id)
    with connect() as conn:
        row = conn.execute('SELECT * FROM "RecordCreationCandidate" WHERE id=%s', (identifier,)).fetchone()
    if row is None or row['state'] != 'reserved' or row['importedNotionId']:
        raise MergeHeld('取り込み結果が未確定の候補ではありません')
    if page_id == row['sourceId']:
        raise MergeHeld('新規登録元ではなく、取り込みで作成されたページを指定してください')
    page = operations.gateway.page(row['dbKey'], page_id)
    if page.get('archived') or page.get('in_trash'):
        raise MergeHeld('回収対象はアーカイブされています')
    marker = import_marker(identifier, row['tool'], row['dbKey'], row['externalId'])
    blocks = operations.gateway.blocks(row['dbKey'], page_id)
    texts = [''.join(item.get('text', {}).get('content', '') for item in block.get('paragraph', {}).get('rich_text', []))
             for block in blocks if block.get('type') == 'paragraph']
    if marker not in texts:
        raise MergeHeld('取り込み時の外部識別子と確認番号が一致しません')
    expected = row['snapshot'].get('importProperties')
    if not isinstance(expected, dict) or not expected:
        raise MergeHeld('取り込み時の内容を照合できません')
    actual = {name: parse_notion_property_value(page['properties'][name]) for name in expected if name in page['properties']}
    if actual != expected:
        raise MergeHeld('取り込み時と現在の値が異なります。個別確認を続けてください')
    existing = operations.store.get(page_id)
    mapped = operations.store.find_by_external_id(Tool(row['tool']), row['externalId'], db_key=row['dbKey'])
    if existing or mapped:
        raise MergeHeld('既に別の対応表があります。通常の候補比較から確認してください')
    target = operations.dispatcher._targets[Tool(row['tool'])]
    external = target.get_record(row['externalId'], db_key=row['dbKey'])
    if not isinstance(external, dict) or not external:
        raise MergeHeld('元の外部レコードを確認できません')
    snapshot = {'id': identifier, 'pageId': page_id, 'dbKey': row['dbKey'], 'tool': row['tool'],
                'externalId': row['externalId'], 'properties': actual, 'external': external, 'marker': marker}
    return {'snapshot': snapshot, 'hash': digest(snapshot)}


def recover_import(operations, actor_id, identifier, page_id, expected_hash):
    from contextlib import ExitStack
    from src.record_merge.domain import MergeHeld
    from src.record_merge.journal import history
    from src.sync_engine.record_sync_lock import acquire_record_sync_lock
    from src.sync_engine.id_mapping import IdMapping
    from src.sync_operations.product_holds import require_manager
    initial = recovery_preview(operations, actor_id, identifier, page_id)['snapshot']
    with ExitStack() as stack:
        for db, key in sorted({(initial['dbKey'], page_id), (initial['dbKey'], 'external:' + initial['tool'] + ':' + initial['externalId'])}):
            stack.enter_context(acquire_record_sync_lock(operations.store, db, key))
        preview = recovery_preview(operations, actor_id, identifier, page_id)
        if preview['hash'] != expected_hash:
            raise MergeHeld('回収確認後に内容が変わりました')
        # ID保存を先に確定する。対応表保存失敗後も通常の取り込み再開で回収できる。
        with connect() as conn:
            require_manager(conn, actor_id)
            conn.execute('UPDATE "RecordCreationCandidate" SET "importedNotionId"=%s,"updatedAt"=now() WHERE id=%s', (page_id, identifier))
            history(conn, actor_id, identifier, initial, {'state': 'creation_import_recovered', 'targetId': page_id})
        operations.store.upsert(IdMapping(page_id, initial['dbKey'], **{
            'zoho_id' if initial['tool'] == 'zoho' else 'kintone_id': initial['externalId']}), expected_last_synced_at=None)
        with connect() as conn:
            conn.execute('UPDATE "RecordCreationCandidate" SET state=\'imported\',"updatedAt"=now() WHERE id=%s', (identifier,))
        return {'state': 'imported', 'targetId': page_id}
