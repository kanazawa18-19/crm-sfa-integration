"""旧保留は元の現在値と項目を特定できた場合だけ個別に再開する。"""
from contextlib import ExitStack
from ast import literal_eval
from psycopg.types.json import Jsonb
from src.db_schema.base import Tool, PropertyType
from src.db_schema.registry import ALL_SCHEMAS, get_schema
from src.hub_creation.creation_payload import ZOHO_RELATIONS
from src.record_merge.aliases import enabled, resolve_alias, require_record_available
from src.record_merge.domain import MergeHeld, digest, resumable_value
from src.record_merge.journal import history
from src.sync_operations.product_holds import connect, require_manager
from src.sync_engine.outbound_field_mapping import zoho_outbound_field_names, kintone_outbound_field_names
from src.sync_engine.record_sync_lock import acquire_record_sync_lock
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value


def matches_pending_value(raw, value):
    label = value.get('name') or value.get('id') if isinstance(value, dict) else value
    if str(label or '').strip() == raw.strip():
        return True
    # 旧台帳はルックアップ辞書全体を文字列で保存していた。IDが同じ場合だけ扱う。
    if not isinstance(value, dict) or not value.get('id') or len(raw) > 4096:
        return False
    try:
        old = literal_eval(raw)
    except (ValueError, SyntaxError, RecursionError):
        return False
    return isinstance(old, dict) and str(old.get('id', '')) == str(value['id'])


def confirmed_resolution(source_tool, source_record_id, target_db_key, raw_value):
    if not enabled(): return None
    with connect() as conn:
        row = conn.execute('''SELECT "targetNotionId" FROM "RelationResolution"
            WHERE "sourceTool"=%s AND "sourceRecordId"=%s AND "targetDbKey"=%s AND "rawValue"=%s''',
            (source_tool, source_record_id, target_db_key, raw_value)).fetchone()
    if row:
        return resolve_alias(target_db_key, 'notion', row['targetNotionId']) or row['targetNotionId']
    return None


def retain_relations(before, canonical):
    """未解決の関連だけを補い、すでに確定している他の関連を消さない。"""
    if not isinstance(before, list) or any(not isinstance(item, str) for item in before):
        raise MergeHeld('現在の関連を全件確認できません')
    desired = list(dict.fromkeys([*before, canonical]))
    if len(desired) > 100:
        raise MergeHeld('関連が100件を超えるため個別確認が必要です')
    return desired


class RelationDecisions:
    def __init__(self, operations):
        self.operations = operations
        self.store = operations.store
        self.dispatcher = operations.dispatcher
        self.gateway = operations.gateway

    def queue_row(self, actor_id, review_id):
        self.operations.authorize(actor_id)
        with connect() as conn:
            row = conn.execute('SELECT * FROM "RelationReviewQueue" WHERE id=%s', (review_id,)).fetchone()
        if row is None or row['status'] != 'pending':
            raise MergeHeld('未処理の関連保留がありません')
        return row

    def source_options(self, row, *, match_raw=True, source_db=''):
        tool = Tool(row['sourceTool'])
        target = self.dispatcher._targets.get(tool)
        if tool not in {Tool.ZOHO, Tool.KINTONE} or target is None:
            raise MergeHeld('元ツールの読取りに対応していません')
        options = []
        tables = zoho_outbound_field_names() if tool == Tool.ZOHO else kintone_outbound_field_names()
        for schema in ALL_SCHEMAS:
            if source_db and schema.key != source_db:
                continue
            mapping = self.store.find_by_external_id(tool, row['sourceRecordId'], db_key=schema.key)
            if mapping is None: continue
            native = target.get_record(row['sourceRecordId'], db_key=schema.key)
            if not isinstance(native, dict): continue
            for prop in schema.properties:
                if prop.property_type != PropertyType.RELATION or prop.relation_target != row['targetDbKey']:
                    continue
                field = tables.get(schema.key, {}).get(prop.name)
                if tool == Tool.ZOHO:
                    for code, (name, _) in ZOHO_RELATIONS.get(schema.key, {}).items():
                        if prop.name == name: field = code
                    # 新規作成の必須関連とは別に、既存案件の提案サービスを照合する。
                    if schema.key == 'project' and prop.name == 'サービス・商品':
                        field = 'field72'
                elif schema.key == 'action' and prop.name == '👨‍👩‍👧‍👦 取引先マスター':
                    field = 'client_name'
                if not field or field not in native: continue
                value = native[field]
                if match_raw and not matches_pending_value(row['rawValue'], value): continue
                options.append({'dbKey': schema.key, 'notionId': mapping.notion_key, 'property': prop.name,
                                'field': field, 'native': value, 'recordVersion': native.get('Modified_Time') or native.get('$revision'),
                                'externalId': row['sourceRecordId'], 'tool': row['sourceTool']})
        if not options:
            raise MergeHeld('元の現在値・アプリ・項目を特定できません。元ツールで確認して再通知してください')
        return options

    def compare(self, actor_id, review_id, target_id, source_db='', property_name=''):
        row = self.queue_row(actor_id, review_id)
        options = self.source_options(row, source_db=source_db) if source_db else self.source_options(row)
        chosen = [item for item in options if (not source_db or item['dbKey'] == source_db)
                  and (not property_name or item['property'] == property_name)]
        if len(chosen) != 1:
            return {'sourceOptions': options, 'needsSourceChoice': True}
        source = chosen[0]
        canonical = resolve_alias(row['targetDbKey'], 'notion', target_id) or target_id
        target_mapping = self.store.get(canonical)
        target_page = self.gateway.page(row['targetDbKey'], canonical)
        if target_page.get('archived') or target_page.get('in_trash'):
            raise MergeHeld('関連先がアーカイブされています')
        native_id = source['native'].get('id') if isinstance(source['native'], dict) else None
        if native_id and (target_mapping is None or str(target_mapping.zoho_id) != str(native_id)):
            raise MergeHeld('元の確定した外部IDと選択先が一致しません')
        candidates = {resolve_alias(row['targetDbKey'], 'notion', identifier) or identifier for identifier in row['candidateNotionPageIds']}
        if not native_id and canonical not in candidates:
            raise MergeHeld('記録された候補から選んでください。候補なしの場合は元データを修正して再通知してください')
        with acquire_record_sync_lock(self.store, source['dbKey'], source['notionId']):
            page = self.gateway.page(source['dbKey'], source['notionId'])
            if page.get('archived') or page.get('in_trash'): raise MergeHeld('元のNotionページがアーカイブされています')
            before = parse_notion_property_value(page['properties'][source['property']])
        snapshot = {'reviewId': review_id, 'source': source, 'targetDbKey': row['targetDbKey'],
                    'targetId': canonical, 'targetName': parse_notion_property_value(target_page['properties'][next(p.name for p in get_schema(row['targetDbKey']).properties if p.property_type == PropertyType.TITLE)]),
                    'rawValue': row['rawValue'], 'before': before, 'desired': retain_relations(before, canonical)}
        return {'snapshot': snapshot, 'hash': digest(snapshot), 'sourceOptions': options}

    def approve(self, actor_id, review_id, expected_hash, target_id, source_db, property_name):
        preview = self.compare(actor_id, review_id, target_id, source_db, property_name)
        if preview.get('hash') != expected_hash:
            raise MergeHeld('比較内容が変更されています')
        snapshot = preview['snapshot']
        source = snapshot['source']
        keys = {(source['dbKey'], source['notionId']), (snapshot['targetDbKey'], snapshot['targetId'])}
        with ExitStack() as stack:
            for db, page in sorted(keys): stack.enter_context(acquire_record_sync_lock(self.store, db, page))
            with connect() as conn:
                require_manager(conn, actor_id)
                previous = conn.execute('SELECT * FROM "RelationReviewDecision" WHERE "reviewId"=%s FOR UPDATE', (review_id,)).fetchone()
                if previous:
                    if previous['snapshotHash'] != expected_hash:
                        raise MergeHeld('別の確認内容で処理が開始されています')
                else:
                    conn.execute('''INSERT INTO "RelationReviewDecision" ("reviewId",snapshot,"snapshotHash","actorId")
                        VALUES (%s,%s,%s,%s)''', (review_id, Jsonb(snapshot), expected_hash, actor_id))
                    history(conn, actor_id, review_id, {}, {'state': 'relation_approved', 'snapshot': snapshot})
            return self._execute(actor_id, snapshot)

    def _execute(self, actor_id, snapshot):
        source = snapshot['source']; review_id = snapshot['reviewId']
        require_record_available(source['dbKey'], source['notionId'])
        require_record_available(snapshot['targetDbKey'], snapshot['targetId'])
        target_page = self.gateway.page(snapshot['targetDbKey'], snapshot['targetId'])
        title_name = next(p.name for p in get_schema(snapshot['targetDbKey']).properties if p.property_type == PropertyType.TITLE)
        if target_page.get('archived') or target_page.get('in_trash') or parse_notion_property_value(target_page['properties'][title_name]) != snapshot['targetName']:
            raise MergeHeld('確認後に選択先が変わりました')
        native_id = source['native'].get('id') if isinstance(source['native'], dict) else None
        target_mapping = self.store.get(snapshot['targetId'])
        if native_id and (target_mapping is None or str(target_mapping.zoho_id) != str(native_id)):
            raise MergeHeld('確認後に選択先の外部ID対応が変わりました')
        row = self.queue_row(actor_id, review_id)
        current_sources = self.source_options(row, match_raw=False, source_db=source['dbKey'])
        mapping = self.store.get(source['notionId'])
        page = self.gateway.page(source['dbKey'], source['notionId'])
        if page.get('archived') or page.get('in_trash') or mapping is None:
            raise MergeHeld('元のNotionページと対応表を確認できません')
        current = parse_notion_property_value(page['properties'][source['property']])
        if source not in current_sources:
            identity = lambda item: {key: value for key, value in item.items() if key not in {'recordVersion', 'native'}}
            same_value = any(identity(item) == identity(source) and (
                item['native'] == source['native'] or
                (source['tool'] == 'kintone' and item['native'] == snapshot['targetName'])
            ) for item in current_sources)
            if current != snapshot['desired'] or not same_value:
                raise MergeHeld('確認後に元ツールの値または版が変わりました')
        if resumable_value(current, snapshot['before'], snapshot['desired']):
            self.gateway.clients[source['dbKey']].update_page(source['notionId'], {source['property']: snapshot['desired']})
        page = self.gateway.page(source['dbKey'], source['notionId'])
        if parse_notion_property_value(page['properties'][source['property']]) != snapshot['desired']:
            raise MergeHeld('関連付けの結果を確認できません')
        self.dispatcher.propagate_linked_relation(mapping, source['property'], snapshot['desired'],
                                                   notion_record=self.dispatcher._targets[Tool.NOTION].get_record(source['notionId'], db_key=source['dbKey']))
        with connect() as conn:
            require_manager(conn, actor_id)
            conn.execute('''INSERT INTO "RelationResolution" ("sourceTool","sourceRecordId","targetDbKey","rawValue","targetNotionId","reviewId")
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT ("sourceTool","sourceRecordId","targetDbKey","rawValue")
                DO UPDATE SET "targetNotionId"=EXCLUDED."targetNotionId","reviewId"=EXCLUDED."reviewId"''',
                (source['tool'], source['externalId'], snapshot['targetDbKey'], snapshot['rawValue'], snapshot['targetId'], review_id))
            conn.execute('UPDATE "RelationReviewQueue" SET status=\'resolved\',"resolvedAt"=now(),"resolvedNotionPageId"=%s WHERE id=%s',
                         (snapshot['targetId'], review_id))
            conn.execute('UPDATE "RelationReviewDecision" SET state=\'done\',"updatedAt"=now() WHERE "reviewId"=%s', (review_id,))
            history(conn, actor_id, review_id, snapshot, {'state': 'relation_resolved'})
        return {'id': review_id, 'state': 'resolved'}

    def resume(self, actor_id, review_id):
        self.operations.authorize(actor_id)
        with connect() as conn:
            decision = conn.execute('SELECT * FROM "RelationReviewDecision" WHERE "reviewId"=%s', (review_id,)).fetchone()
        if decision is None: raise MergeHeld('承認済みの処理がありません')
        if decision['state'] == 'done': return {'id': review_id, 'state': 'resolved'}
        snapshot = decision['snapshot']; source = snapshot['source']
        with ExitStack() as stack:
            for db, page in sorted({(source['dbKey'], source['notionId']), (snapshot['targetDbKey'], snapshot['targetId'])}):
                stack.enter_context(acquire_record_sync_lock(self.store, db, page))
            return self._execute(actor_id, snapshot)
