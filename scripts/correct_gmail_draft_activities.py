"""固定した下書き4版と送信1通を、既存活動1件へ訂正する運用専用処理。

previewは読取のみ。applyは同じ計画hashで再開し、CRMへの新規POSTは行わない。
顧客値・対象IDは私有入力から渡し、一般の重複判定は変更しない。
"""
from __future__ import annotations

import argparse
import copy
import json
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL

from psycopg.types.json import Jsonb
from src.db_schema.base import Tool, PropertyType
from src.db_schema.registry import ALL_SCHEMAS, get_schema
from src.email_review import domain
from src.email_review.repository import Repository
from src.gmail_sync.mail_state import sent
from src.record_merge.application import MergeService
from src.record_merge.domain import MergeHeld, digest, copy_block, replace_relation
from src.record_merge.journal import MergeJournal
from src.record_merge.notion_gateway import NotionMergeGateway
from src.sync_engine.record_sync_lock import acquire_record_sync_locks
from src.sync_operations.product_holds import connect, require_manager

KIND = 'gmail_draft_correction'


def serial(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def require(condition, reason):
    if not condition:
        raise MergeHeld(reason)


def plan_hash(plan):
    return digest({key: value for key, value in plan.items() if key != 'planHash'})


def validate_evidence(rows, evidence, sent_id, canonical_id):
    require(len(rows) == 5 and len({r['id'] for r in rows}) == 5, '固定5受付だけを指定してください')
    by_id = {r['id']: r for r in rows}
    require(sent_id in by_id and canonical_id in by_id and sent_id != canonical_id, '送信と正本の指定が不正です')
    require(len({r['owner'] for r in rows}) == 1 and len({r['message'].get('thread_id') for r in rows}) == 1
            and bool(rows[0]['message'].get('thread_id')), '同じ所有者・会話の5版ではありません')
    require(len({r['message']['id'] for r in rows}) == 5 and len({r.get('action_id') for r in rows}) == 5
            and all(r.get('action_id') for r in rows), 'メールIDまたは活動IDが不正です')
    require(len(evidence) == 5 and {e['review_id'] for e in evidence} == set(by_id), '履歴証拠と受付の集合が違います')
    require(len({r.get('contact_id') for r in rows}) == 1 and bool(rows[0].get('contact_id')),
            '同じ連絡先の5版ではありません')
    drafts = []
    sent_history = None
    for item in evidence:
        row = by_id[item['review_id']]
        require(row.get('direction') == 'outbound' and row.get('canonical_key') == domain.identity(row['message'], row['owner']),
                'メール共通IDを確認できません')
        require(item.get('gmail_id') == row['message']['id'] and item.get('thread_id') == row['message']['thread_id']
                and all(e.get('messageId') == row['message']['id'] and e.get('threadId') == row['message']['thread_id']
                        for e in item['events']), '履歴イベントとメール実体のIDが一致しません')
        additions = [e for e in item['events'] if e['kind'] == 'messagesAdded']
        require(len(additions) == 1, 'メッセージ追加履歴が一意ではありません')
        addition = additions[0]
        if row['id'] == sent_id:
            require('SENT' in (addition.get('messageLabels') or []) and 'DRAFT' not in (addition.get('messageLabels') or [])
                    and sent(row['message']), '最終送信の証拠がありません')
            sent_history = int(addition['historyId'])
        else:
            removed = [e for e in item['events'] if e['kind'] == 'messagesDeleted' and 'DRAFT' in (e.get('messageLabels') or [])]
            require('DRAFT' in (addition.get('messageLabels') or []) and len(removed) == 1, '下書き追加・削除を確認できません')
            start, end = int(addition['historyId']), int(removed[0]['historyId'])
            require(start < end, '下書き履歴の順序が不正です')
            drafts.append((start, end))
    drafts.sort()
    require(all(a[1] < b[0] for a, b in zip(drafts, drafts[1:])) and drafts[-1][1] < sent_history,
            '4下書きから最終送信への順序を確認できません')
    return by_id


class Journal:
    """共通操作履歴へ一意の段階を予約し、通信断後も再送前に実読する。"""
    def __init__(self, plan, actor):
        self.plan, self.actor = plan, actor
        self.operation = str(uuid5(NAMESPACE_URL, KIND + ':' + plan['planHash']))

    def identifier(self, stage):
        return str(uuid5(NAMESPACE_URL, self.operation + ':' + stage))

    def state(self, stage):
        with connect() as conn:
            require_manager(conn, self.actor)
            row = conn.execute('SELECT * FROM "SyncOperationHistory" WHERE id=%s', (self.identifier(stage),)).fetchone()
        if row:
            require(row['kind'] == KIND and row['subjectId'] == self.operation
                    and row['beforeState']['planHash'] == self.plan['planHash'], '操作予約が競合しています')
        return row['afterState']['state'] if row else None

    def reserve(self, stage):
        with connect() as conn:
            require_manager(conn, self.actor)
            conn.execute('''INSERT INTO "SyncOperationHistory"
                (id,"actorId",kind,"subjectId","beforeState","afterState") VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING''', (self.identifier(stage), self.actor, KIND, self.operation,
                Jsonb({'planHash': self.plan['planHash'], 'stage': stage,
                       **({'plan': self.plan} if stage == 'freeze' else {})}), Jsonb({'state': 'reserved'})))
        require(self.state(stage) in {'reserved', 'done'}, '操作予約を確認できません')

    def done(self, stage):
        with connect() as conn:
            require_manager(conn, self.actor)
            changed = conn.execute('''UPDATE "SyncOperationHistory" SET "afterState"=%s
                WHERE id=%s AND kind=%s AND "beforeState"->>'planHash'=%s RETURNING id''',
                (Jsonb({'state': 'done'}), self.identifier(stage), KIND, self.plan['planHash'])).fetchone()
            require(changed is not None, '段階の完了を保存できません')


class FixedGateway(NotionMergeGateway):
    """固定ページで実読した空の管理外関連だけを扱う。一般統合には使わない。"""
    def __init__(self, clients, store, allowed, expected_empty=None):
        super().__init__(clients, store)
        self.allowed = set(allowed)
        self.expected_empty = expected_empty

    def verify_empty(self):
        found = {}
        known = {p.name for p in get_schema('action').properties}
        for page_id in sorted(self.allowed):
            raw = self.clients['action'].get_raw_page(page_id)
            unknown = {name: prop for name, prop in raw['properties'].items()
                       if prop.get('type') == 'relation' and name not in known}
            for prop in unknown.values():
                require(prop.get('relation') == [] and prop.get('has_more') is False, '管理外の関連が空ではありません')
            found[page_id] = sorted(unknown)
        if self.expected_empty is not None:
            require(found == self.expected_empty, '管理外関連の定義が変わりました')
        return found

    def reciprocals(self, db_key):
        require(db_key == 'action', '活動以外は対象外です')
        self.verify_empty()
        from src.sync_engine.clients._http import raise_for_error, ApiError
        response = self.clients[db_key]._request('GET', '/databases/' + get_schema(db_key).notion_database_id)
        raise_for_error(response, ApiError)
        properties = response.json()['properties']
        databases = {s.notion_database_id.replace('-', ''): s.key for s in ALL_SCHEMAS}
        result = {}
        for name, prop in properties.items():
            relation = prop.get('relation', {})
            if prop.get('type') != 'relation' or relation.get('type') != 'dual_property':
                continue
            other = databases.get(relation.get('database_id', '').replace('-', ''))
            other_name = relation.get('dual_property', {}).get('synced_property_name')
            if not other or not other_name:
                require(all(name in names for names in self.verify_empty().values()), '未確認の管理外関連があります')
                continue
            result[name] = {'dbKey': other, 'property': other_name}
        return result

    def verify_identity(self, job):
        self.verify_empty()
        super().verify_identity(job)

    def verify_archive_ready(self, job):
        self.verify_empty()
        super().verify_archive_ready(job)


def run(plan, io, journal):
    require(plan.get('kind') == KIND and plan_hash(plan) == plan.get('planHash'), '計画hashが一致しません')
    validate_evidence(plan['reviews'], plan['evidence'], plan['sentReviewId'], plan['canonicalReviewId'])
    markers = [r.get('correction_hold') or {} for r in plan['reviews']]
    require(all(m.get('active') is True and m.get('key') and m.get('blocked_action_ids') == [r['action_id']]
                for r, m in zip(plan['reviews'], markers)) and len({m['key'] for m in markers}) == 1,
            '訂正予約前の計画は実行できません')
    with io.locks(plan):
        io.verify_identity(plan)
        # doneも実読する。先行段階の成功だけで全体を完了扱いにしない。
        for stage in ['freeze', 'merge', 'canonical', 'retire', 'handoff', 'email_logs']:
            state = journal.state(stage)
            if state == 'done':
                io.verify(stage, plan)
                continue
            journal.reserve(stage)
            io.apply(stage, plan)
            io.verify(stage, plan)
            journal.done(stage)
        io.verify_final(plan)
        if journal.state('release') != 'done':
            journal.reserve('release')
            io.release(plan)
            io.verify('release', plan)
            journal.done('release')
        io.verify('release', plan)
        return {'state': 'done', 'planHash': plan['planHash']}


class LiveIO:
    def __init__(self, wiring, actor):
        self.wiring, self.actor = wiring, actor
        self.creation = wiring.creation_service
        require(self.creation is not None, '本番の共通登録接続がありません')
        self.store, self.clients = self.creation.store, self.creation.notion_clients
        self.targets = wiring.dispatcher._dispatcher._targets

    def locks(self, plan):
        keys = {('action', row['action_id']) for row in plan['reviews']}
        keys.update(('contact', row['contact_id']) for row in plan['reviews'])
        keys.update((child['dbKey'], child['id']) for item in plan.get('merges', [])
                    for child in item['snapshot']['children'])
        return acquire_record_sync_locks(self.store, keys)

    def rows(self, ids, conn=None, lock=False):
        if conn is None:
            with connect() as connection:
                return self.rows(ids, connection, lock)
        rows = conn.execute('SELECT data FROM "EmailReview" WHERE id=ANY(%s) ORDER BY id' + (' FOR UPDATE' if lock else ''), (ids,)).fetchall()
        return {r['data']['id']: r['data'] for r in rows}

    def native(self, tool, identifier, page_id):
        target = self.targets[Tool(tool)]._resolve('action')
        if tool == 'kintone':
            return target._client.get_record(target._app, identifier)
        if tool == 'zoho':
            # このカスタムDBの通常GETは更新日時を返さない。前後のCOQLで同じ版を確認する。
            before_version = self.zoho_version(target, identifier)
            value = target._client.get_record(target._module, identifier)
            after_version = self.zoho_version(target, identifier)
            require(before_version == after_version, 'Zohoの読取中に版が変わりました')
            if value is None:
                require(after_version is None, 'Zohoの存在確認が一致しません')
                return None
            require(after_version is not None and str(value.get('id')) == identifier, 'ZohoのIDまたは版を確認できません')
            return {**value, 'Modified_Time': after_version['Modified_Time']}
        row = target.find_unique_row_by_sync_key(page_id)
        require(row is not None and str(row) == str(identifier), 'シート行番号または同期キーが変わりました')
        value = target._client.get_row(target._sheet, row)
        require(value is not None and str(value.get('同期キー', '')).replace('-', '') == page_id.replace('-', ''), 'シートの同期キーが一致しません')
        return value

    @staticmethod
    def zoho_version(target, identifier):
        from src.sync_engine.clients._http import raise_for_error, ApiError
        require(identifier.isdigit() and target._module.replace('_', '').isalnum(), 'Zoho識別子が不正です')
        response = target._client._request('POST', '/coql', json_body={'select_query':
            f"select id, Modified_Time from {target._module} where id = '{identifier}'"}, idempotent=True)
        if response.status_code == 204:
            return None
        raise_for_error(response, ApiError)
        data = response.json().get('data')
        require(isinstance(data, list) and len(data) == 1 and str(data[0].get('id')) == identifier
                and bool(data[0].get('Modified_Time')) and not response.json().get('info', {}).get('more_records'),
                'Zoho更新時刻を一意に取得できません')
        return data[0]

    def extras(self, tool, identifier):
        from src.sync_engine.clients._http import request_with_retry, raise_for_error, ApiError
        target = self.targets[Tool(tool)]._resolve('action')
        if tool == 'kintone':
            client = target._client
            response = request_with_retry('GET', f'https://{client._domain}/k/v1/record/comments.json',
                headers=client._headers(has_json_body=False), params={'app': target._app, 'record': identifier, 'limit': 10})
            raise_for_error(response, ApiError)
            body = response.json()
            require(body.get('comments') == [] and not body.get('older') and not body.get('newer'), 'コメント付きの活動は個別に保持してください')
            response = client._request('GET', params={'app': target._app, 'id': identifier})
            raise_for_error(response, ApiError)
            raw = response.json()
            require(not any(v.get('type') == 'FILE' and v.get('value') for v in raw['record'].values()), '添付付きの活動は個別に保持してください')
            return {'raw': raw, 'comments': body}
        if tool == 'zoho':
            result = {}
            for related in ('Attachments', 'Notes'):
                response = target._client._request('GET', f'/{target._module}/{identifier}/{related}?page=1&per_page=200')
                if response.status_code == 204:
                    result[related] = []
                    continue
                raise_for_error(response, ApiError)
                body = response.json()
                require(body.get('data') == [] and not body.get('info', {}).get('more_records'), '関連本文・添付付きの活動は個別に保持してください')
                result[related] = []
            return result
        return {}

    def reserve(self, spec):
        """配備済みガードを使い、比較前に固定5受付・ページの通常処理を止める。"""
        rows = list(self.rows(spec['reviewIds']).values())
        validate_evidence(rows, spec['evidence'], spec['sentReviewId'], spec['canonicalReviewId'])
        key = digest(spec)
        with self.locks({'reviews': rows}):
            with connect() as conn:
                require_manager(conn, self.actor)
                current = self.rows(spec['reviewIds'], conn, True)
                require(current == {r['id']:r for r in rows}, '予約中に受付が変わりました')
                for old in rows:
                    marker = old.get('correction_hold')
                    expected = {'key':key, 'active':True, 'blocked_action_ids':[old['action_id']]}
                    if marker == expected:
                        continue
                    require(marker is None and old['state'] not in {'processing','evaluating','reconciling','recheck','unknown'},
                            '処理中または別の訂正予約があるため待って再確認してください')
                    new = {**old, 'correction_hold':expected, 'state':'held', 'revision':old['revision']+1,
                           'reason':'固定訂正を予約しました。通常操作は停止中です',
                           'actor':self.actor, 'channel':'gmail_draft_correction'}
                    Repository().save(conn, old, new)
        return {'state':'reserved', 'reservationHash':key, 'count':len(rows)}

    def build(self, spec):
        with connect() as conn:
            require_manager(conn, self.actor)
        rows = list(self.rows(spec['reviewIds']).values())
        by_id = validate_evidence(rows, spec['evidence'], spec['sentReviewId'], spec['canonicalReviewId'])
        require(all(r.get('correction_hold') == {'key':digest(spec), 'active':True,
                    'blocked_action_ids':[r['action_id']]} for r in rows), '先に固定5件の訂正予約が必要です')
        require(by_id[spec['sentReviewId']].get('delivery_details', {}).get('contact') == 'delivered',
                '連絡先の配送完了を先に確認してください')
        plan = {'kind': KIND, 'reviews': rows, 'evidence': spec['evidence'],
                'sentReviewId': spec['sentReviewId'], 'canonicalReviewId': spec['canonicalReviewId']}
        with self.locks(plan):
            pages = [r['action_id'] for r in rows]
            gateway = FixedGateway(self.clients, self.store, pages)
            plan['emptyRelations'] = gateway.verify_empty()
            plan['mappings'] = {page: serial(asdict(m)) if (m := self.store.get(page)) else None for page in pages}
            for mapping in plan['mappings'].values():
                if mapping:
                    mapping.pop('last_synced_at', None)
            canonical = by_id[spec['canonicalReviewId']]
            canonical_page = canonical['action_id']
            sent_row = by_id[spec['sentReviewId']]
            mapping = plan['mappings'][canonical_page]
            require(mapping and all(mapping.get(k) for k in ('zoho_id', 'kintone_id', 'spreadsheet_row')), '正本の全3先IDが必要です')
            require(plan['mappings'][sent_row['action_id']] is None, '送信活動に既存外部対応があります')
            plan['canonicalPage'] = canonical_page
            plan['merges'] = []
            plan['initialSnapshots'] = []
            # 最初に送信版を正本へ移し、その後は正本値を保持して残る下書きを統合する。
            ordered = [sent_row] + [r for r in rows if r['id'] not in {sent_row['id'], canonical['id']}]
            desired = None
            title = next(p.name for p in get_schema('action').properties if p.property_type == PropertyType.TITLE)
            invariant = None
            cumulative_blocks = None
            prior_sources = []
            final_children = {}
            for row in ordered:
                snapshot = gateway.snapshot('action', row['action_id'], canonical_page)
                plan['initialSnapshots'].append(copy.deepcopy(snapshot))
                if cumulative_blocks is None:
                    cumulative_blocks = snapshot['targetBlocks']
                snapshot['targetBlocks'] = cumulative_blocks
                for child in snapshot['children']:
                    for previous in prior_sources:
                        child['before'] = replace_relation(child['before'], previous, canonical_page)
                for values in (snapshot['source'], snapshot['target']):
                    current = {k: v for k, v in values.items() if k not in {title, '履歴メモ'}}
                    if invariant is None:
                        invariant = current
                    require(current == invariant, '親・担当者・種別・日時等に違いがあり固定訂正できません')
                snapshot['workflow'] = KIND
                snapshot['emptyRelations'] = plan['emptyRelations']
                if desired is not None:
                    snapshot['target'] = desired
                choices = {key: 'source' if desired is None else 'target'
                           for key in set(snapshot['source']) | set(snapshot['target'])}
                steps = gateway.plan(snapshot, choices)
                desired = steps[0]['desired']
                cumulative_blocks = cumulative_blocks + snapshot['sourceBlocks']
                prior_sources.append(row['action_id'])
                for step in steps:
                    if step['kind'] == 'relation':
                        final_children[(step['dbKey'], step['id'], step['property'])] = step
                plan['merges'].append({'snapshot': snapshot, 'steps': steps})
            plan['desiredNotion'] = desired
            plan['desiredBlocks'] = cumulative_blocks
            plan['finalChildren'] = list(final_children.values())
            plan['external'] = []
            title = next(p.name for p in get_schema('action').properties if p.property_type == PropertyType.TITLE)
            changes = {key: desired[key] for key in (title, '履歴メモ', 'アクション日')}
            from src.sync_engine.clients.spreadsheet_client import _to_cell_value
            for row in rows:
                page = row['action_id']
                mapping = plan['mappings'][page]
                if not mapping:
                    continue
                for tool, column in [('kintone', 'kintone_id'), ('zoho', 'zoho_id'), ('spreadsheet', 'spreadsheet_row')]:
                    identifier = mapping.get(column)
                    if not identifier:
                        continue
                    before = self.native(tool, str(identifier), page)
                    require(before is not None, '外部活動が見つかりません')
                    extra = self.extras(tool, str(identifier))
                    canonical_record = page == canonical_page
                    update = None
                    if canonical_record:
                        if tool == 'kintone':
                            old_body = canonical['written_properties']['履歴メモ']
                            comment = before.get('comment')
                            require(isinstance(comment, str) and old_body and comment.count(old_body) == 1, 'kintone本文を一意に置換できません')
                            update = {'comment': comment.replace(old_body, changes['履歴メモ'])}
                        elif tool == 'zoho':
                            update = self.targets[Tool.ZOHO]._resolve('action')._to_zoho_payload(changes, 'action')
                            require(update and len(update) == len(changes), 'Zohoの訂正項目が揃いません')
                        else:
                            require(all(key in before for key in changes), 'シートの訂正列がありません')
                            update = {key: _to_cell_value(value, field_name=key) for key, value in changes.items()}
                    else:
                        if tool == 'spreadsheet':
                            require('削除フラグ' in before and before['削除フラグ'] in (None, '', False), 'シートの取消状態を確認できません')
                            update = {'削除フラグ': True}
                        elif tool == 'kintone':
                            require(str(before.get('$revision', '')).isdigit(), 'kintoneの版がありません')
                        else:
                            require(bool(before.get('Modified_Time')), 'Zohoの更新時刻がありません')
                    plan['external'].append({'tool': tool, 'id': str(identifier), 'page': page,
                        'canonical': canonical_record, 'before': serial(before), 'update': update, 'extra': serial(extra)})
            with connect() as conn:
                draft_ids = [r['message']['id'] for r in rows if r['id'] != sent_row['id']]
                logs = conn.execute('SELECT * FROM "EmailLog" WHERE "gmailMessageId"=ANY(%s) ORDER BY id', (draft_ids,)).fetchall()
                require(len(logs) == 4, '下書き4件のEmailLogが揃いません')
                reminders = conn.execute('SELECT * FROM "EmailReminderLog" WHERE "emailLogId"=ANY(%s)', ([r['id'] for r in logs],)).fetchall()
                require(not reminders, '下書きへのリマインド参照を個別に保持してください')
                aliases = conn.execute('SELECT * FROM "RecordMergeAlias" WHERE "dbKey"=\'action\' AND ("oldId"=ANY(%s) OR "targetId"=ANY(%s))', (pages, pages)).fetchall()
                require(not aliases, '既存統合があります')
                plan['emailLogs'] = serial(logs)
                plan['sentEmailLog'] = serial(conn.execute('SELECT * FROM "EmailLog" WHERE "gmailMessageId"=%s', (sent_row['message']['id'],)).fetchone())
                require(plan['sentEmailLog'] is not None, '送信EmailLogがありません')
            require(self.rows(list(by_id)) == by_id, '比較中に受付が変わりました')
        # 比較で見つかった親・子の鍵も確保し直し、無保護だった読取を再確認する。
        with self.locks(plan):
            self.verify_identity(plan)
            gateway = self.gateway(plan)
            for snapshot in plan['initialSnapshots']:
                gateway.verify_identity({'snapshot': snapshot, 'sourceId': snapshot['sourceId'],
                    'targetId': snapshot['targetId'], 'dbKey': 'action', 'steps': []})
                require(gateway.children('action', snapshot['sourceId']) == snapshot['children'],
                        '全対象を施錠する前に親・子の参照が変わりました')
            first = plan['initialSnapshots'][0]
            require(gateway.values('action', gateway.page('action', canonical_page)) == first['target']
                    and [copy_block(b) for b in gateway.blocks('action', canonical_page)] == first['targetBlocks'],
                    '全対象を施錠する前に正本が変わりました')
            require(self.rows(list(by_id)) == by_id, '全対象を施錠する前に受付が変わりました')
        plan['planHash'] = plan_hash(plan)
        return plan

    def gateway(self, plan):
        return FixedGateway(self.clients, self.store, [r['action_id'] for r in plan['reviews']], plan['emptyRelations'])

    def verify_identity(self, plan):
        from src.record_merge.aliases import resolve_alias
        self.gateway(plan).verify_empty()
        for page, expected in plan['mappings'].items():
            alias = resolve_alias('action', 'notion', page)
            require(alias in (None, plan['canonicalPage']), '別の正本へ統合されています')
            mapping = self.store.get(page)
            actual = serial(asdict(mapping)) if mapping else None
            if actual:
                actual.pop('last_synced_at', None)
            require(actual == (plan['mappings'][plan['canonicalPage']] if alias else expected), '外部ID対応が変わりました')

    def expected_rows(self, plan, handed=False, released=False):
        rows = {}
        for old in plan['reviews']:
            value = copy.deepcopy(old)
            value.update(state='held', revision=old['revision'] + 1, reason='下書き誤登録の訂正処理中です')
            if old['id'] != plan['sentReviewId']:
                value['message']['label_ids'] = ['DRAFT']
            if handed:
                value['revision'] += 1
                if old['id'] == plan['sentReviewId']:
                    value.update(state='done', delivery='delivered', action_id=plan['canonicalPage'],
                        original_action_id=old['action_id'], reason='送信済み1通へ下書き誤登録を統合・訂正しました',
                        delivery_details={'action':'delivered','contact':old.get('delivery_details',{}).get('contact')})
                    require(value['delivery_details']['contact'] == 'delivered', '連絡先の配送が未完了です')
                else:
                    value.update(state='excluded', reason='送信前の下書き誤登録を取消し、最終送信活動へ統合しました。配送状態は取消前の履歴です',
                        canonical_action_id=plan['canonicalPage'], original_action_id=old['action_id'])
                    if old['action_id'] == plan['canonicalPage']:
                        value['action_id'] = None
            if released:
                value['revision'] += 1
                marker = copy.deepcopy(value.get('correction_hold') or {})
                marker['active'] = old['id'] != plan['sentReviewId']
                marker['blocked_action_ids'] = [] if old['action_id'] == plan['canonicalPage'] else [old['action_id']]
                value['correction_hold'] = marker
            rows[old['id']] = value
        return rows

    def review_change(self, plan, handed=False):
        initial = {row['id']:row for row in plan['reviews']}
        before = self.expected_rows(plan) if handed else initial
        desired = self.expected_rows(plan, handed)
        with connect() as conn:
            require_manager(conn, self.actor)
            actual = self.rows(list(initial), conn, True)
            if actual == desired:
                return
            require(actual == before, '受付が比較時から変わりました')
            # 一意action_idを解放してからSENTへ移す。同一トランザクションなので空白期間はない。
            order = sorted(initial, key=lambda key: key == plan['sentReviewId'])
            for key in order:
                Repository().save(conn, actual[key], desired[key])

    def release(self, plan):
        desired = self.expected_rows(plan, True, True)
        with connect() as conn:
            require_manager(conn, self.actor)
            actual = self.rows(list(desired), conn, True)
            if actual == desired:
                return
            require(actual == self.expected_rows(plan, True), '最終解除前に受付が変わりました')
            for key in desired:
                Repository().save(conn, actual[key], desired[key])

    def merge(self, plan):
        gateway, journal = self.gateway(plan), MergeJournal()
        for item in plan['merges']:
            snapshot = {**item['snapshot'], 'correctionPlanHash':plan['planHash']}
            with connect() as conn:
                jobs = conn.execute('''SELECT * FROM "RecordMergeJob" WHERE snapshot->>'correctionPlanHash'=%s
                    AND "sourceId"=%s''', (plan['planHash'], snapshot['sourceId'])).fetchall()
            require(len(jobs) <= 1, '統合予約が重複しています')
            job = jobs[0] if jobs else journal.create(snapshot, item['steps'], self.actor)
            if job['state'] == 'draft':
                gateway.verify_identity(job)
                for step in item['steps']:
                    if step['kind'] == 'append_body':
                        gateway.verify_before(step)
                    else:
                        require(gateway.read(step) == step['before'], '統合予約後に実値が変わりました')
                journal.decide(job['id'], self.actor, job['planHash'], 'approve')
            # 通常画面からは専用jobを再開させず、固定空関連を再確認するgatewayだけで進める。
            MergeService(journal, gateway, lambda _: nullcontext()).execute(job['id'], self.actor)

    @staticmethod
    def native_equal(actual, expected, *, tool=None):
        if actual is None or expected is None:
            return actual is expected
        volatile = {'$revision','Modified_Time','Modified_By','更新日時','更新者'}
        left = {k:v for k,v in actual.items() if k not in volatile}
        right = {k:v for k,v in expected.items() if k not in volatile}
        if tool == 'zoho':
            from src.sync_engine.outbound_field_mapping import zoho_outbound_field_names
            body_field = zoho_outbound_field_names().get('action', {}).get('履歴メモ')
            body = right.get(body_field) if body_field else None
            current = left.get(body_field) if body_field else None
            # 実読で確認した「末尾LFがちょうど1つ除かれる」場合だけ許容する。
            if (isinstance(body, str) and isinstance(current, str) and current
                    and body.endswith('\n') and not body.endswith(('\n\n', '\r\n'))
                    and current == body[:-1]):
                right[body_field] = current
            # 今回の更新時刻と同値になった活動時刻だけをシステム更新として扱う。
            if ('Last_Activity_Time' in left and 'Last_Activity_Time' in right
                    and left['Last_Activity_Time'] and left['Last_Activity_Time'] == actual.get('Modified_Time')):
                left.pop('Last_Activity_Time')
                right.pop('Last_Activity_Time')
        return left == right

    def desired_external(self, item):
        if item['update'] is None:
            return None
        return {**item['before'], **item['update']}

    def write_external(self, item):
        from src.sync_engine.clients._http import request_with_retry, raise_for_error, ApiError
        actual = self.native(item['tool'], item['id'], item['page'])
        desired = self.desired_external(item)
        if self.native_equal(actual, desired, tool=item['tool']):
            return
        require(actual == item['before'], '外部活動が比較後に変わりました')
        require(self.extras(item['tool'], item['id']) == item['extra'], '外部の添付・コメントが変わりました')
        target = self.targets[Tool(item['tool'])]._resolve('action')
        if item['tool'] == 'kintone':
            if item['update'] is not None:
                target._client.update_record(target._app, item['id'], item['update'], expected_version=str(actual['$revision']))
            else:
                client = target._client
                response = request_with_retry('DELETE', f'https://{client._domain}/k/v1/records.json',
                    headers=client._headers(has_json_body=True),
                    json_body={'app':target._app, 'ids':[item['id']], 'revisions':[str(actual['$revision'])]},
                    idempotent=False, max_retries=0)
                raise_for_error(response, ApiError)
        elif item['tool'] == 'zoho':
            if item['update'] is not None:
                response = target._client._request('PUT', f'/{target._module}/{item["id"]}',
                    json_body={'data':[{'id':item['id'], **item['update']}], 'trigger':[]},
                    extra_headers={'If-Unmodified-Since':actual['Modified_Time']}, idempotent=False)
            else:
                response = target._client._request('DELETE', f'/{target._module}?ids={item["id"]}&wf_trigger=false',
                    extra_headers={'If-Unmodified-Since':actual['Modified_Time']}, idempotent=False)
            raise_for_error(response, ApiError)
            result = response.json()['data']
            require(len(result) == 1 and result[0].get('code') == 'SUCCESS'
                    and str(result[0].get('details',{}).get('id')) == item['id'], 'Zohoの処理結果を確認できません')
        else:
            target._client.update_row(target._sheet, int(item['id']), item['update'])

    def external_change(self, plan, canonical):
        # 各既知IDの段階も予約し、一部成功からの再開で余分な再更新・再取消をしない。
        journal = Journal(plan, self.actor)
        for item in plan['external']:
            if item['canonical'] != canonical:
                continue
            stage = 'external:' + item['tool'] + ':' + item['id']
            previous = journal.state(stage)
            if previous is None:
                require(self.native(item['tool'], item['id'], item['page']) == item['before'],
                        '取消・訂正の予約前に実物が変わりました')
                require(self.extras(item['tool'], item['id']) == item['extra'], '予約前に添付・コメントが変わりました')
            journal.reserve(stage)
            if previous != 'done':
                self.write_external(item)
            require(self.native_equal(self.native(item['tool'],item['id'],item['page']), self.desired_external(item), tool=item['tool']),
                    '外部訂正の読戻しが一致しません')
            journal.done(stage)

    @staticmethod
    def log_evidence(row):
        return {key:value for key,value in row.items() if key != 'digestedAt'} if row else row

    def remove_draft_logs(self, plan):
        with connect() as conn:
            require_manager(conn,self.actor)
            ids=[r['id'] for r in plan['emailLogs']]
            rows=conn.execute('SELECT * FROM "EmailLog" WHERE id=ANY(%s) ORDER BY id FOR UPDATE',(ids,)).fetchall()
            if not rows:
                return
            require([self.log_evidence(r) for r in serial(rows)]==[self.log_evidence(r) for r in plan['emailLogs']],'下書きEmailLogが変わりました')
            require(not conn.execute('SELECT 1 FROM "EmailReminderLog" WHERE "emailLogId"=ANY(%s)',(ids,)).fetchall(),
                    'リマインド参照が作られています')
            # digestedAtの最新値も削除と同じトランザクションで退避する。
            journal = Journal(plan, self.actor)
            conn.execute('''INSERT INTO "SyncOperationHistory"
                (id,"actorId",kind,"subjectId","beforeState","afterState") VALUES (%s,%s,%s,%s,%s,%s)''',
                (journal.identifier('email_logs_observed'), self.actor, KIND, journal.operation,
                 Jsonb({'planHash':plan['planHash'], 'rows':serial(rows)}), Jsonb({'state':'done'})))
            # 全行はfreeze予約のbeforeState.planにも保存済み。集計対象からのみ取り除く。
            conn.execute('DELETE FROM "EmailLog" WHERE id=ANY(%s)',(ids,))

    def apply(self, stage, plan):
        self.verify_identity(plan)
        if stage == 'freeze': self.review_change(plan)
        elif stage == 'merge': self.merge(plan)
        elif stage == 'canonical': self.external_change(plan, True)
        elif stage == 'retire':
            self.verify('canonical',plan)
            self.verify('merge',plan)
            self.external_change(plan,False)
        elif stage == 'handoff': self.review_change(plan,True)
        elif stage == 'email_logs': self.remove_draft_logs(plan)
        else: raise MergeHeld('未対応の段階です')

    def verify(self, stage, plan):
        if stage in {'freeze','handoff','release'}:
            actual=self.rows([r['id'] for r in plan['reviews']])
            require(actual==self.expected_rows(plan,True,True)
                    or (stage!='release' and actual==self.expected_rows(plan,True))
                    or (stage=='freeze' and actual==self.expected_rows(plan)),
                    '受付の訂正結果が一致しません')
        elif stage=='merge':
            from src.record_merge.aliases import resolve_alias
            gateway=self.gateway(plan)
            gateway.verify_empty()
            require(gateway.values('action',gateway.page('action',plan['canonicalPage']))==plan['desiredNotion'],
                    '最終送信の正本内容が一致しません')
            require([copy_block(b) for b in gateway.blocks('action', plan['canonicalPage'])] == plan['desiredBlocks'],
                    '各版の追跡用本文を保持できていません')
            for child in plan['finalChildren']:
                gateway.verify_completed(child)
            for item in plan['merges']:
                source=item['snapshot']['sourceId']
                require(gateway.page('action',source).get('archived') is True
                        and resolve_alias('action','notion',source)==plan['canonicalPage'], '旧活動の退避・対応が未完了です')
                for tool,identifier in item['snapshot']['aliases'].items():
                    if identifier:
                        require(resolve_alias('action',tool,str(identifier))==plan['canonicalPage'],'外部旧IDの再取込防止が未完了です')
        elif stage in {'canonical','retire'}:
            for item in plan['external']:
                if item['canonical'] != (stage=='canonical'): continue
                require(self.native_equal(self.native(item['tool'],item['id'],item['page']),self.desired_external(item), tool=item['tool']),
                        '外部実物の訂正結果が一致しません')
        elif stage=='email_logs':
            with connect() as conn:
                require(not conn.execute('SELECT 1 FROM "EmailLog" WHERE id=ANY(%s)',([r['id'] for r in plan['emailLogs']],)).fetchall(),'下書きが集計に残っています')
                actual=conn.execute('SELECT * FROM "EmailLog" WHERE id=%s',(plan['sentEmailLog']['id'],)).fetchone()
                require(self.log_evidence(serial(actual))==self.log_evidence(plan['sentEmailLog']),'実在送信のEmailLogが変わりました')

    def verify_final(self, plan):
        self.verify_identity(plan)
        for stage in ('merge','canonical','retire','handoff','email_logs'):
            self.verify(stage,plan)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=['reserve','preview','apply'])
    parser.add_argument('--input',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--actor',required=True)
    parser.add_argument('--expected-hash')
    args=parser.parse_args()
    from scripts.backfill_zoho_missed_records import load_env
    load_env(True)
    import os
    require(bool(os.environ.get('DATABASE_URL_UNPOOLED')),'直接接続DBが必要です')
    os.environ['DATABASE_URL']=os.environ['DATABASE_URL_UNPOOLED']
    from src.sync_engine.production_wiring import get_production_wiring
    io=LiveIO(get_production_wiring(),args.actor)
    data=json.loads(args.input.read_text())
    if args.mode=='reserve':
        require(args.expected_hash == digest(data), '予約対象specの承認hashが必要です')
        result=io.reserve(data)
    elif args.mode=='preview':
        require(args.expected_hash is None,'previewには承認hashを指定しません')
        result=io.build(data)
    else:
        require(args.expected_hash==data.get('planHash') and plan_hash(data)==args.expected_hash,'承認済みhashが必要です')
        result=run(data,io,Journal(data,args.actor))
    with open(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as output:
        output.write(json.dumps(result,ensure_ascii=False,indent=2,default=str))
    print(json.dumps({'mode':args.mode,'planHash':result.get('planHash'),'reservationHash':result.get('reservationHash'),'state':result.get('state','preview')}))


if __name__=='__main__':
    main()
