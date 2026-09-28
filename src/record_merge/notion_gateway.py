"""Notionの比較・本文保持・子参照変更。読めない情報があれば元を残す。"""
from __future__ import annotations
from dataclasses import asdict
from urllib.parse import urlencode
from src.db_schema.base import PropertyType
from src.db_schema.registry import ALL_SCHEMAS, get_schema
from src.sync_engine.clients._http import raise_for_error
from src.sync_engine.clients.notion_client import NotionApiError
from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value, PARSEABLE_NOTION_PROPERTY_TYPES
from src.record_merge.domain import MergeHeld, choose_properties, copy_block, digest, replace_relation, block_count


class NotionMergeGateway:
    def __init__(self, clients, store):
        self.clients, self.store = clients, store

    def page(self, db_key, page_id):
        schema = get_schema(db_key)
        relations = {p.name for p in schema.properties if p.property_type == PropertyType.RELATION}
        raw = self.clients[db_key].get_raw_page_with_relations(page_id, relations)
        if raw.get('parent', {}).get('database_id', '').replace('-', '') != schema.notion_database_id.replace('-', ''):
            raise MergeHeld('対象ページのデータベースが異なります')
        return raw

    def values(self, db_key, raw):
        schema = get_schema(db_key)
        result = {}
        for name, value in raw['properties'].items():
            kind = value.get('type')
            if kind == 'files':
                if value.get('files'):
                    raise MergeHeld('プロパティ添付は保持方法を確認するまで統合できません: ' + name)
                continue
            if kind not in PARSEABLE_NOTION_PROPERTY_TYPES:
                continue
            if kind == 'date' and isinstance(value.get('date'), dict) and value['date'].get('end'):
                raise MergeHeld('期間のある日付は個別に保持してください: ' + name)
            if kind in {'title', 'rich_text'}:
                for text in value.get(kind, []):
                    annotations = text.get('annotations', {})
                    if text.get('type', 'text') != 'text' or text.get('text', {}).get('link') or any(
                        item not in (False, 'default') for item in annotations.values()
                    ):
                        raise MergeHeld('書式・リンクを含む項目は個別に保持してください: ' + name)
            parsed = parse_notion_property_value(value)
            try:
                prop = schema.get_property(name)
            except KeyError:
                if parsed not in (None, '', [], {}):
                    raise MergeHeld('対応が未定義の項目があります: ' + name)
                continue
            if prop.is_writable:
                result[name] = parsed
        return result

    def blocks(self, db_key, page_id, *, depth=0, budget=None):
        if depth > 1:
            raise MergeHeld('深い本文構造は個別に保持してください')
        budget = [0] if budget is None else budget
        client = self.clients[db_key]
        result, cursor, seen = [], None, set()
        while True:
            params = {'page_size': 100}
            if cursor: params['start_cursor'] = cursor
            response = client._request('GET', f'/blocks/{page_id}/children?' + urlencode(params))
            raise_for_error(response, NotionApiError)
            body = response.json()
            if not isinstance(body.get('results'), list) or not isinstance(body.get('has_more'), bool):
                raise MergeHeld('本文の取得完了を確認できません')
            for block in body['results']:
                budget[0] += 1
                if budget[0] > 100:
                    raise MergeHeld('本文が100ブロックを超えるため個別に保持してください')
                if block.get('has_children'):
                    block['children'] = self.blocks(db_key, block['id'], depth=depth+1, budget=budget)
                result.append(block)
            if not body.get('has_more'):
                break
            cursor = body.get('next_cursor')
            if not cursor or cursor in seen:
                raise MergeHeld('本文の続きを確認できません')
            seen.add(cursor)
        return result

    def children(self, db_key, source_id):
        result = []
        for schema in ALL_SCHEMAS:
            for prop in schema.properties:
                if prop.property_type != PropertyType.RELATION or prop.relation_target != db_key:
                    continue
                if schema.key not in self.clients:
                    raise MergeHeld('子の関連を確認する接続がありません: ' + schema.key)
                body = self.clients[schema.key].query_raw({'filter': {'property': prop.name,
                    'relation': {'contains': source_id}}, 'page_size': 100})
                if not isinstance(body.get('results'), list) or not isinstance(body.get('has_more'), bool):
                    raise MergeHeld('子の関連の取得完了を確認できません')
                if body.get('request_status') is not None and not isinstance(body['request_status'], dict):
                    raise MergeHeld('子の関連の取得状態が不正です')
                # 1万件の暗黙切捨ても避け、限定数を超える場合は元を保持する。
                if body.get('has_more') or (body.get('request_status') or {}).get('type') == 'incomplete':
                    raise MergeHeld('子の関連が多いため分割した確認が必要です')
                for item in body.get('results', []):
                    raw = self.clients[schema.key].get_raw_page_with_relations(item['id'], {prop.name})
                    ids = parse_notion_property_value(raw['properties'][prop.name])
                    if source_id not in ids:
                        raise MergeHeld('比較中に子の関連が変わりました')
                    title = next(p.name for p in schema.properties if p.property_type == PropertyType.TITLE)
                    name = parse_notion_property_value(raw['properties'][title]) if title in raw['properties'] else item['id']
                    result.append({'dbKey': schema.key, 'id': item['id'], 'name': name, 'property': prop.name, 'before': ids})
        return sorted(result, key=lambda row: (row['dbKey'], row['id'], row['property']))

    def reciprocals(self, db_key):
        response = self.clients[db_key]._request('GET', '/databases/' + get_schema(db_key).notion_database_id)
        raise_for_error(response, NotionApiError)
        properties = response.json().get('properties')
        if not isinstance(properties, dict):
            raise MergeHeld('双方向関連の定義を確認できません')
        databases = {schema.notion_database_id.replace('-', ''): schema.key for schema in ALL_SCHEMAS}
        result = {}
        for name, prop in properties.items():
            relation = prop.get('relation', {})
            if prop.get('type') != 'relation' or relation.get('type') != 'dual_property':
                continue
            other_db = databases.get(relation.get('database_id', '').replace('-', ''))
            other_name = relation.get('dual_property', {}).get('synced_property_name')
            if not other_db or not other_name:
                raise MergeHeld('管理外または未確定の双方向関連があります: ' + name)
            result[name] = {'dbKey': other_db, 'property': other_name}
        return result

    def snapshot(self, db_key, source_id, target_id):
        source, target = self.page(db_key, source_id), self.page(db_key, target_id)
        if any(row.get('archived') or row.get('in_trash') for row in (source, target)):
            raise MergeHeld('アーカイブされていない2件を選んでください')
        mappings = [self.store.get(identifier) for identifier in (source_id, target_id)]
        if mappings[1] is None or any(mapping is not None and mapping.db_key != db_key for mapping in mappings):
            raise MergeHeld('2件の同期先対応を確認してください')
        if mappings[0] and any(getattr(mappings[0], name) and not getattr(mappings[1], name) for name in ('zoho_id', 'kintone_id')):
            raise MergeHeld('外部登録のあるページを残す先に選んでください')
        blocks = [copy_block(block) for block in self.blocks(db_key, source_id)]
        target_blocks = [copy_block(block) for block in self.blocks(db_key, target_id)]
        from src.hub_creation.creation_payload import ZOHO_RELATIONS
        single_relations = {name for name, _ in ZOHO_RELATIONS.get(db_key, {}).values()} if mappings[1].zoho_id else set()
        return {'dbKey': db_key, 'sourceId': source_id, 'targetId': target_id,
                'singleRelations': sorted(single_relations),
                'unionFields': [p.name for p in get_schema(db_key).properties if p.property_type in {PropertyType.RELATION, PropertyType.MULTI_SELECT} and p.name not in single_relations],
                'source': self.values(db_key, source), 'target': self.values(db_key, target),
                'sourceEditedAt': source['last_edited_time'], 'targetEditedAt': target['last_edited_time'],
                'sourceBlocks': blocks, 'targetBlocks': target_blocks,
                'children': self.children(db_key, source_id), 'reciprocals': self.reciprocals(db_key),
                'mappings': [{key: value for key, value in asdict(mapping).items() if key != 'last_synced_at'} if mapping else None for mapping in mappings],
                'aliases': {'notion': source_id, 'zoho': mappings[0].zoho_id if mappings[0] else None, 'kintone': mappings[0].kintone_id if mappings[0] else None}}

    def plan(self, snapshot, choices):
        db_key, target_id, source_id = snapshot['dbKey'], snapshot['targetId'], snapshot['sourceId']
        if block_count(snapshot['sourceBlocks']) + block_count(snapshot['targetBlocks']) > 100:
            raise MergeHeld('統合後の本文が100ブロックを超えるため個別に保持してください')
        for prop in get_schema(db_key).properties:
            if prop.property_type == PropertyType.RELATION and prop.relation_target == db_key:
                linked = set(snapshot['source'].get(prop.name) or []) | set(snapshot['target'].get(prop.name) or [])
                if linked & {source_id, target_id}:
                    raise MergeHeld('同じDB内で統合元・先を相互参照しています。関連を個別に確認してください')
        unions = set(snapshot['unionFields'])
        desired = choose_properties(snapshot['source'], snapshot['target'], choices, union_fields=unions)
        for name in snapshot.get('singleRelations', []):
            if len(desired.get(name) or []) > 1:
                raise MergeHeld('外部の単一関連欄に残す1件を確認してください: ' + name)
        for prop in get_schema(db_key).properties:
            if prop.property_type == PropertyType.RELATION and prop.relation_target == db_key and prop.name in desired:
                desired[prop.name] = replace_relation(desired[prop.name] or [], source_id, target_id)
        steps = [{'kind': 'properties', 'dbKey': db_key, 'id': target_id,
                  'before': snapshot['target'], 'desired': desired}]
        if snapshot['sourceBlocks']:
            steps.append({'kind': 'append_body', 'dbKey': db_key, 'id': target_id,
                          'before': snapshot['targetBlocks'], 'desired': snapshot['sourceBlocks']})
        for child in snapshot['children']:
            if child['dbKey'] == db_key and child['id'] in {source_id, target_id}:
                continue
            # 統合先自身の参照も他の子と同じく全件を保持して付け替える。
            intermediate = list(child['before'])
            for parent_name, reciprocal in snapshot['reciprocals'].items():
                if reciprocal == {'dbKey': child['dbKey'], 'property': child['property']} and child['id'] in desired.get(parent_name, []):
                    intermediate = list(dict.fromkeys(intermediate + [target_id]))
            steps.append({'kind': 'relation', **child, 'intermediate': intermediate,
                          'desired': replace_relation(child['before'], source_id, target_id)})
        steps.append({'kind': 'archive', 'dbKey': db_key, 'id': source_id, 'before': False, 'desired': True})
        return steps


    def verify_identity(self, job):
        snapshot = job['snapshot']
        for identifier, expected in zip((job['sourceId'], job['targetId']), snapshot['mappings']):
            mapping = self.store.get(identifier)
            actual = {key: value for key, value in asdict(mapping).items() if key != 'last_synced_at'} if mapping else None
            if actual != expected:
                raise MergeHeld('統合中に同期先の対応が変わりました')
        source = self.page(job['dbKey'], job['sourceId'])
        # アーカイブ済みからの再開でも内容の保持を確認する。
        expected_source = dict(snapshot['source'])
        for name, reciprocal in snapshot.get('reciprocals', {}).items():
            removed = set()
            for step in job['steps']:
                if step['kind'] == 'relation' and reciprocal == {'dbKey': step['dbKey'], 'property': step['property']}:
                    current = self.read(step)
                    if set(current) == set(step['desired']):
                        removed.add(step['id'])
            if removed:
                expected_source[name] = [identifier for identifier in expected_source.get(name, []) if identifier not in removed]
        if self.values(job['dbKey'], source) != expected_source:
            raise MergeHeld('比較後に統合元の内容が変わりました')
        if source.get('archived') or source.get('in_trash'):
            # Notionはアーカイブ後の本文取得に404を返す。事前検証を経た予約と、
            # それより前の全手順の完了記録がある場合だけ統合先の検証へ進む。
            steps = job['steps']
            archive = steps[-1] if steps else {}
            progress = job.get('progress', {})
            if (archive.get('kind') != 'archive' or archive.get('id') != job['sourceId']
                    or archive.get('desired') is not True
                    or progress.get(str(len(steps)-1), {}).get('state') not in {'reserved', 'done'}
                    or any(progress.get(str(i), {}).get('state') != 'done' for i in range(len(steps)-1))):
                raise MergeHeld('アーカイブ前の保持確認と実行予約を確認できません')
        elif [copy_block(block) for block in self.blocks(job['dbKey'], job['sourceId'])] != snapshot['sourceBlocks']:
            raise MergeHeld('比較後に統合元の本文が変わりました')

    def read(self, step):
        raw = self.page(step['dbKey'], step['id'])
        if step['kind'] == 'archive':
            return bool(raw.get('archived') or raw.get('in_trash'))
        if raw.get('archived') or raw.get('in_trash'):
            raise MergeHeld('変更先がアーカイブされています')
        if step['kind'] == 'relation':
            return parse_notion_property_value(raw['properties'][step['property']])
        if step['kind'] == 'properties':
            return self.values(step['dbKey'], raw)
        raise MergeHeld('手順の読取り形式が不正です')

    def write(self, step):
        client = self.clients[step['dbKey']]
        if step['kind'] == 'archive':
            client.archive_page(step['id'])
        else:
            properties = {step['property']: step['desired']} if step['kind'] == 'relation' else step['desired']
            client.update_page(step['id'], properties)

    def verify_before(self, step):
        current = [copy_block(block) for block in self.blocks(step['dbKey'], step['id'])]
        if current != step['before']:
            raise MergeHeld('比較後に統合先の本文が変わりました')

    def append_body(self, step):
        response = self.clients[step['dbKey']]._request('PATCH', f"/blocks/{step['id']}/children",
            json_body={'children': step['desired']}, idempotent=False)
        raise_for_error(response, NotionApiError)
        return [block['id'] for block in response.json().get('results', [])]

    def verify_receipt(self, step, receipt):
        if len(receipt) != len(step['desired']):
            raise MergeHeld('本文追加の結果件数を確認できません')
        self.verify_completed(step)

    def verify_completed(self, step):
        if step['kind'] == 'append_body':
            current = [copy_block(block) for block in self.blocks(step['dbKey'], step['id'])]
            if current != step['before'] + step['desired']:
                raise MergeHeld('追加後の本文を確認できません')
        elif self.read(step) != step['desired']:
            raise MergeHeld('統合後の値が変更されています')

    def verify_archive_ready(self, job):
        self.verify_identity(job)
        for step in job['steps']:
            if step['kind'] != 'archive':
                self.verify_completed(step)
        remaining = [child for child in self.children(job['dbKey'], job['sourceId'])
                     if not (child['dbKey'] == job['dbKey'] and child['id'] == job['sourceId'])]
        if remaining:
            raise MergeHeld('元を参照する子が残っています。元を保持して再確認してください')
