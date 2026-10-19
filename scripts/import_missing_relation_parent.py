"""確定済みZoho親IDの取込漏れだけを、予約付きでNotionへ一度取り込む。

既定は私有計画の保存。--apply でも関連の変更・外部CRM作成は行わない。
結果不明の reserved は再POSTせず停止する。恒常APIには組み込まない。
"""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import unicodedata

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.hub_creation.domain import require_notion_fields
from src.record_merge.domain import MergeHeld, digest, stable_blocks
from src.sync_engine.id_mapping import IdMapping
from src.sync_engine.new_record_builder import build_notion_properties_for_new_record
from src.sync_engine.record_sync_lock import acquire_record_sync_lock


class ParentImport:
    def __init__(self, operations, journal, actor_id):
        from src.record_merge.relation_decisions import RelationDecisions
        self.op, self.journal, self.actor = operations, journal, actor_id
        self.dec = RelationDecisions(operations)
        self.store = operations.store
        self.client = operations.gateway.clients['client_master']

    def source_key(self, external_id):
        return 'relation-parent-import:zoho:' + external_id

    def mappings(self, external_id):
        # find_by_external_idの先頭1件では、重複した対応表を見逃す。
        from src.sync_engine.notion_id_mapping import (
            _EXTERNAL_ID_PROPERTIES, _text_equals_filter, _select_equals_filter, _page_to_mapping,
        )
        return [_page_to_mapping(p) for p in self.store._query_all({'and': [
            _text_equals_filter(_EXTERNAL_ID_PROPERTIES[Tool.ZOHO], external_id),
            _select_equals_filter('db_key', 'client_master'),
        ]})]

    def title_candidates(self, title):
        # 文字列自体は変換しない。検索だけで全角空白と文字列\\u3000の別表記も拾う。
        normalized = unicodedata.normalize('NFKC', title.replace('\\u3000', ' '))
        terms = {title, normalized} - {''}
        filters = [{'property': '取引先名', 'title': {'contains': term}} for term in sorted(terms)]
        # 英単語一語だけの検索は別施設まで拾う。日本語表記は全ての連続部分をANDで照合。
        japanese = sorted(set(re.findall(r'[一-龯ぁ-んァ-ヶー]+', normalized)))
        if japanese:
            parts = [{'property': '取引先名', 'title': {'contains': term}} for term in japanese]
            filters.append(parts[0] if len(parts) == 1 else {'and': parts})
        pages = self.client.query_all_pages(filter={'or': filters})
        return {p['id'] for p in pages}

    def snapshot(self, review_id, external_id, *, imported=None):
        from src.record_merge.aliases import resolve_alias, require_record_available
        self.op.authorize(self.actor)
        if not external_id.isdigit():
            raise MergeHeld('Zoho親IDの形式が不正です')
        row = self.dec.queue_row(self.actor, review_id)
        if row['sourceTool'] != 'zoho' or row['targetDbKey'] != 'client_master':
            raise MergeHeld('Zoho取引先の関連保留だけが対象です')
        options = self.dec.source_options(row, source_db='action')
        if len(options) != 1:
            raise MergeHeld('元の関連を一意に特定できません')
        source = options[0]
        if source['field'] != 'field6' or str((source.get('native') or {}).get('id')) != external_id:
            raise MergeHeld('元アクションの確定親IDが一致しません')
        require_record_available('action', source['notionId'])
        page = self.op.gateway.page('action', source['notionId'])
        if page.get('archived') or page.get('in_trash'):
            raise MergeHeld('元アクションがアーカイブされています')
        values = self.op.gateway.values('action', page)
        if values.get(source['property']) != []:
            raise MergeHeld('元アクションの関連は空欄ではありません')
        if resolve_alias('client_master', 'zoho', external_id):
            raise MergeHeld('親IDには統合履歴があります')
        mappings = self.mappings(external_id)
        if len(mappings) > 1 or (mappings and mappings[0].notion_key != imported):
            raise MergeHeld('親IDの対応表が既に存在または重複しています')
        native = self.op.dispatcher._targets[Tool.ZOHO].get_record(external_id, db_key='client_master')
        if not native or str(native.get('id')) != external_id:
            raise MergeHeld('Zoho親実物を確認できません')
        properties = build_notion_properties_for_new_record(source_tool=Tool.ZOHO,
            db_key='client_master', external_id=external_id, raw_record=native, id_mapping_store=self.store)
        require_notion_fields('client_master', properties)
        title = properties.get('取引先名')
        if not isinstance(title, str) or not title.strip() or title != native.get('Account_Name'):
            raise MergeHeld('親名称を原文どおりに取り込めません')
        if self.title_candidates(title) - ({imported} if imported else set()):
            raise MergeHeld('同名または部分一致の親候補があります。先に実物を照合してください')
        return {'version': 1, 'reviewId': review_id, 'externalId': external_id,
                'source': source, 'sourceValues': values,
                'sourceBlocks': stable_blocks(self.op.gateway.blocks('action', source['notionId'])),
                'external': native, 'properties': properties}

    def preview(self, review_id, external_id):
        attempt = self.journal.get(self.source_key(external_id), 'notion')
        if attempt:
            raise MergeHeld('取込予約が既にあります。元の計画で再開してください')
        snapshot = self.snapshot(review_id, external_id)
        return {'snapshot': snapshot, 'hash': digest(snapshot)}

    @staticmethod
    def marker(plan):
        return '関連先取込の確認番号: ' + plan['hash']

    def verify_imported(self, plan, identifier):
        from src.record_merge.aliases import require_record_available, resolve_alias
        from src.sync_engine.clients.notion_client import build_notion_properties
        from src.sync_engine.webhook_handlers.notion_webhook import parse_notion_property_value
        require_record_available('client_master', identifier)
        if resolve_alias('client_master', 'notion', identifier):
            raise MergeHeld('取込ページが統合されています')
        page = self.op.gateway.page('client_master', identifier)
        if page.get('archived') or page.get('in_trash'):
            raise MergeHeld('取込ページがアーカイブされています')
        current = self.store.get(identifier)
        if current and (current.db_key != 'client_master' or
                        current.zoho_id != plan['snapshot']['externalId']):
            raise MergeHeld('取込ページの外部ID対応が一致しません')
        # 対応成立後は通常同期で項目が更新される。初期値への巻き戻しも再作成もしない。
        expected = (build_notion_properties(plan['snapshot']['properties'], get_schema('client_master'))
                    if current is None else {})
        for name, prop in expected.items():
            # 書込形式はtypeを持たない。読取形式にして同じ変換規則で比較する。
            kind = next(iter(prop))
            if kind in ('title', 'rich_text'):
                prop = {kind: [{**part, 'plain_text': part.get('text', {}).get('content', '')}
                               for part in prop[kind]]}
            wanted = parse_notion_property_value({'type': kind, **prop})
            actual = parse_notion_property_value(page['properties'][name])
            if actual != wanted:
                raise MergeHeld('取込ページの項目が計画と一致しません')
        blocks = self.op.gateway.blocks('client_master', identifier)
        marker = self.marker(plan)
        texts = [''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                         for t in b.get('paragraph', {}).get('rich_text', [])) for b in blocks]
        if texts.count(marker) != 1:
            raise MergeHeld('取込ページの確認番号を一意に確認できません')
        return current

    def verify_external_binding(self, plan, imported):
        """POST後は元actionの内容でなく、確定した外部IDの帰属だけを再確認する。"""
        from src.record_merge.aliases import resolve_alias
        external_id = plan['snapshot']['externalId']
        if resolve_alias('client_master', 'zoho', external_id):
            raise MergeHeld('親IDには統合履歴があります')
        native = self.op.dispatcher._targets[Tool.ZOHO].get_record(external_id, db_key='client_master')
        if not native or str(native.get('id')) != external_id:
            raise MergeHeld('同じZoho親IDの実在を確認できません')
        mappings = self.mappings(external_id)
        if len(mappings) > 1 or (mappings and mappings[0].notion_key != imported):
            raise MergeHeld('外部親IDの対応が別ページまたは複数にあります')

    def apply(self, plan):
        snapshot = plan['snapshot']
        if plan['hash'] != digest(snapshot) or snapshot.get('version') != 1:
            raise MergeHeld('計画ファイルの内容が一致しません')
        external_id = snapshot['externalId']
        key = self.source_key(external_id)
        keys = [('action', snapshot['source']['notionId']),
                ('client_master', 'external:zoho:' + external_id)]
        with ExitStack() as stack:
            for db, lock in sorted(keys):
                stack.enter_context(acquire_record_sync_lock(self.store, db, lock))
            self.op.authorize(self.actor)
            attempt = self.journal.get(key, 'notion')
            if attempt and (attempt['dbKey'] != 'client_master' or attempt['identityHash'] != plan['hash']):
                raise MergeHeld('別の計画による取込予約です')
            if attempt and attempt['state'] != 'created':
                raise MergeHeld('取込結果が不明です。自動で再作成せず確認番号で実物を調査してください')
            imported = attempt.get('externalId') if attempt else None
            if attempt and not imported:
                raise MergeHeld('取込済みIDを確認できません')
            if not imported:
                current = self.snapshot(snapshot['reviewId'], external_id)
                if digest(current) != plan['hash']:
                    raise MergeHeld('計画後に元の関連・本文・親実物が変わりました')
                if not self.journal.reserve(key, 'notion', 'client_master', plan['hash']):
                    raise MergeHeld('取込予約が競合しました')
                # 例外・timeoutでもreservedのまま。未作成と推測せず再POSTを禁止する。
                imported = self.client.create_page_once(snapshot['properties'], children=[{
                    'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [
                        {'type': 'text', 'text': {'content': self.marker(plan)}}]}}])
                self.journal.finish(key, 'notion', imported)
            self.verify_external_binding(plan, imported)
            # 作成後と再開時の両方で、確認番号・項目・外部IDの整合を実読する。
            with acquire_record_sync_lock(self.store, 'client_master', imported):
                mapping = self.verify_imported(plan, imported)
                if mapping is None:
                    self.store.upsert(IdMapping(imported, 'client_master', zoho_id=external_id),
                                      expected_last_synced_at=None)
                elif mapping.zoho_id is None:
                    raise MergeHeld('取込ページに別の対応表があります。上書きせず確認してください')
                self.verify_imported(plan, imported)
                matches = self.mappings(external_id)
                if len(matches) != 1 or matches[0].notion_key != imported:
                    raise MergeHeld('保存後の外部ID対応が一意ではありません')
            return {'state': 'imported', 'notionId': imported, 'externalId': external_id}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('--actor-id', required=True)
    parser.add_argument('--review-id')
    parser.add_argument('--external-id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply and (args.review_id or args.external_id):
        parser.error('--apply は保存済み計画だけを使用します。--review-id/--external-id は指定しないでください')
    from scripts.backfill_zoho_missed_records import load_env
    from src.sync_engine.production_wiring import get_production_wiring
    from src.record_merge.operations import MergeOperations
    from src.hub_creation.journal import PostgresCreationJournal
    load_env(False)
    wiring = get_production_wiring()
    service = wiring.creation_service
    operations = MergeOperations(service.store, service.notion_clients, wiring.dispatcher._dispatcher)
    importer = ParentImport(operations, PostgresCreationJournal(), args.actor_id)
    if args.apply:
        result = importer.apply(json.loads(args.plan.read_text()))
        print(json.dumps(result, ensure_ascii=False))
    else:
        if not args.review_id or not args.external_id:
            parser.error('計画には --review-id と --external-id が必要です')
        plan = importer.preview(args.review_id, args.external_id)
        # 顧客原本を含むので、私有領域へ排他的に0600で保存する。
        with os.fdopen(os.open(args.plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'state': 'preview', 'hash': plan['hash']}))


if __name__ == '__main__':
    main()
