"""問い合わせの保存予約と、保存した4項目だけの明示同期。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
from psycopg.types.json import Jsonb

from src.db_schema.base import Tool
from src.hub_creation.journal import PostgresCreationJournal, connect
from src.sync_engine.record_sync_lock import acquire_record_sync_lock
from src.sync_engine.sync_event import SyncEvent

FIELDS = frozenset({'名前', 'メールアドレス', '携帯番号', '取引先マスター'})


class InquiryHandoff:
    def __init__(self, wiring=None, journal=None):
        if wiring is None:
            from src.sync_engine.production_wiring import get_production_wiring
            wiring = get_production_wiring()
        self.wiring = wiring
        self.journal = journal or PostgresCreationJournal()

    @contextmanager
    def session(self, payload):
        identity = payload['email'].lower() if payload['email'] else json.dumps(
            [payload['company'], payload['name'], payload['phone']], ensure_ascii=False)
        key = 'inquiry:' + hashlib.sha256(identity.encode()).hexdigest()
        with acquire_record_sync_lock(self.wiring.id_mapping_store, 'contact', key):
            session = InquirySession(self, key)
            session.remember_payload(payload)
            yield session


class InquirySession:
    def __init__(self, flow, key):
        self.flow, self.key = flow, key

    def remember_payload(self, payload):
        # 曖昧候補でも担当者が原本を照合し、同じ入口へ再投入できるよう入力を保持する。
        original = {key: payload.get(key, '') for key in ('company', 'name', 'email', 'phone')}
        with connect() as conn:
            conn.execute('''INSERT INTO "InquiryHandoff" ("sourceKey","inputPayload") VALUES (%s,%s)
                ON CONFLICT ("sourceKey") DO UPDATE SET "inputPayload"=EXCLUDED."inputPayload"''',
                (self.key, Jsonb(original)))

    def check_page(self, contact, page_id):
        """固定保存先を再利用する前に、退避・統合済みの旧ページを止める。"""
        from src.record_merge.aliases import resolve_alias, require_record_available
        from src.sync_engine.clients.notion_client import NotionApiError
        try:
            try:
                raw = contact.get_raw_page(page_id)
            except NotionApiError as exc:
                if exc.status_code != 404:
                    raise
                raw = {'archived': True}
            reason = ('保存先がアーカイブ・ゴミ箱にあります。正本を確認してください'
                      if raw.get('archived') or raw.get('in_trash') else None)
            if not reason and resolve_alias('contact', 'notion', page_id):
                reason = '保存先が統合済みの旧ページです。正本を確認してください'
            if not reason:
                require_record_available('contact', page_id)
        except Exception:
            self.stage(page_id, {})
            self.flow.journal.hold('notion:' + page_id, 'inquiry_handoff', 'contact', '保存先の状態を確認できません')
            self._state('blocked', '保存先の状態を確認できません')
            raise
        if reason:
            self.stage(page_id, {})
            self.flow.journal.hold('notion:' + page_id, 'inquiry_handoff', 'contact', reason)
            self._state('blocked', reason)
            return {'state': 'blocked', 'reason': reason}
        return None

    def read(self):
        with connect() as conn:
            return conn.execute('SELECT * FROM "InquiryHandoff" WHERE "sourceKey"=%s', (self.key,)).fetchone()

    def saved_page(self):
        row = self.read()
        if row and row['pageId']:
            return row['pageId']
        attempt = self.flow.journal.get(self.key, 'inquiry_notion')
        if attempt and attempt['state'] == 'created':
            return attempt['externalId']
        if attempt and attempt['state'] == 'reserved':
            raise RuntimeError('Notion作成結果が不明です。再作成せず確認を待っています')
        return None

    def stage(self, page_id, properties):
        if set(properties) - FIELDS:
            raise ValueError('問い合わせの同期対象外項目があります')
        with connect() as conn:
            row = conn.execute('SELECT * FROM "InquiryHandoff" WHERE "sourceKey"=%s FOR UPDATE', (self.key,)).fetchone()
            if row and row['pageId'] and page_id and row['pageId'] != page_id:
                raise ValueError('問い合わせの保存先が競合しています')
            merged = dict(row['properties']) if row and row['state'] != 'done' else {}
            merged.update(properties)
            if row and not properties and row['pageId'] == page_id:
                return
            if row and row['state'] == 'done' and row['properties'] == properties and row['pageId'] == page_id:
                return
            conn.execute('''INSERT INTO "InquiryHandoff" ("sourceKey","pageId",properties)
                VALUES (%s,%s,%s) ON CONFLICT ("sourceKey") DO UPDATE SET
                "pageId"=coalesce(EXCLUDED."pageId","InquiryHandoff"."pageId"),
                properties=EXCLUDED.properties,state='pending',reason='',"updatedAt"=now()''',
                (self.key, page_id, Jsonb(merged)))
            if page_id:
                # 保存予定と由来の印を同時確定し、途中障害後の通常通知も保護する。
                conn.execute('''INSERT INTO "HubCreationAttempt" ("sourceKey",target,"dbKey",state,reason)
                    VALUES (%s,'inquiry_handoff','contact','blocked','問い合わせ保存後の同期確認待ち')
                    ON CONFLICT ("sourceKey",target) DO NOTHING''', ('notion:' + page_id,))
                conn.execute('''DELETE FROM "HubCreationAttempt" WHERE "sourceKey"=%s
                    AND target='inquiry_handoff' AND state='blocked' ''', (self.key,))

    @contextmanager
    def page_lock(self, page_id):
        with acquire_record_sync_lock(self.flow.wiring.id_mapping_store, 'contact', page_id):
            yield

    def ambiguous(self, reason):
        self.stage(None, {})
        self.flow.journal.hold(self.key, 'inquiry_handoff', 'contact', reason)
        self._state('blocked', reason)
        return {'state': 'blocked', 'reason': reason}

    def create(self, contact, properties):
        self.stage(None, properties)
        if not self.flow.journal.reserve(self.key, 'inquiry_notion', 'contact', self.key):
            raise RuntimeError('Notionへの作成予約を確認してください')
        page = contact.create_page_once(properties)
        self.flow.journal.finish(self.key, 'inquiry_notion', page)
        self.stage(page, properties)
        return page

    def _state(self, state, reason, *, zoho_reason=None):
        with connect() as conn:
            conn.execute('''UPDATE "InquiryHandoff" SET state=%s,reason=%s,
                "zohoHoldReason"=CASE WHEN %s='done' THEN NULL ELSE coalesce(%s,"zohoHoldReason") END,
                "updatedAt"=now() WHERE "sourceKey"=%s''',
                         (state, reason, state, zoho_reason, self.key))

    def deliver(self, contact, page_id, *, additional_hold=None):
        invalid = self.check_page(contact, page_id)
        if invalid:
            return invalid
        row = self.read()
        if not row:
            # 更新差分がない既存ページも、入口が未追跡なら正規の保留判定へ載せる。
            self.stage(page_id, {})
            row = self.read()
        if row['state'] == 'done':
            return {'state': 'done', 'reason': ''}
        source = 'notion:' + page_id
        self.flow.journal.hold(source, 'inquiry_handoff', 'contact', '問い合わせ保存後の同期確認待ち')
        current = contact.get_page(page_id)
        if current is None:
            raise RuntimeError('保存済み連絡先の読戻しに失敗しました')
        mapping = self.flow.wiring.id_mapping_store.get(page_id)
        reason = additional_hold
        if not reason and not (mapping and mapping.zoho_id):
            from src.hub_creation.domain import inquiry_creation_hold
            reason = inquiry_creation_hold(current)
        if not reason and any(current.get(name) != value for name, value in row['properties'].items()):
            reason = '保存後に対象項目が変更されています。現在値を確認して引継ぎを再登録してください'
        if reason:
            self.flow.journal.hold(source, 'zoho', 'contact', reason)
            self.flow.journal.hold(source, 'inquiry_handoff', 'contact', reason)
            self._state('blocked', reason, zoho_reason=reason)
            return {'state': 'blocked', 'reason': reason}
        raw = contact.get_raw_page(page_id)
        if raw.get('archived') or raw.get('in_trash'):
            raise RuntimeError('保存先がアーカイブされています')
        occurred = datetime.fromisoformat(raw['last_edited_time'].replace('Z', '+00:00'))
        result = self.flow.wiring.dispatcher.dispatch(SyncEvent(
            source_tool=Tool.NOTION, db_key='contact', external_id=page_id,
            occurred_at=occurred, properties=dict(row['properties']),
            expected_source_properties=dict(row['properties']),
        ))
        if result.skipped or result.has_partial_skips:
            reason = '通常同期に未完了があります。項目別台帳と登録保留を確認してください'
            self.flow.journal.hold(source, 'inquiry_handoff', 'contact', reason)
            self._state('blocked', reason)
            return {'state': 'blocked', 'reason': reason}
        mapping = self.flow.wiring.id_mapping_store.get(page_id)
        zoho_hold = self.flow.journal.get(source, 'zoho')
        # この入口が残した同じ理由の保留だけを、確定対応と配送成功後に終端にする。
        if (mapping and mapping.zoho_id and row['zohoHoldReason'] and zoho_hold
                and zoho_hold['state'] == 'blocked' and zoho_hold['reason'] == row['zohoHoldReason']):
            self.flow.journal.resolve_hold(source, 'zoho', mapping.zoho_id,
                                           expected_reason=row['zohoHoldReason'])
        self.flow.journal.dismiss_hold(source, 'inquiry_handoff')
        self._state('done', '')
        return {'state': 'done', 'reason': ''}


def enqueue_existing_inquiry(page_id, properties, *, contact, wiring=None, additional_hold=None):
    """確認済みの固定対象だけを、通常入口と同じ台帳へ登録・処理する。"""
    flow = InquiryHandoff(wiring=wiring)
    with connect() as conn:
        rows = conn.execute('SELECT "sourceKey" FROM "InquiryHandoff" WHERE "pageId"=%s ORDER BY "sourceKey"',
                            (page_id,)).fetchall()
    # 通常入口で残った行を、その由来理由ごと再開する。別の復旧行を増やさない。
    keys = [row['sourceKey'] for row in rows] or ['inquiry-recovery:' + page_id]
    results = []
    for key in keys:
        with acquire_record_sync_lock(flow.wiring.id_mapping_store, 'contact', key):
            session = InquirySession(flow, key)
            prior = session.read()
            if prior and prior['state'] == 'done':
                results.append({'state': 'done', 'reason': ''})
                continue
            session.stage(page_id, properties)
            results.append(session.deliver(contact, page_id, additional_hold=additional_hold))
    blocked = [result for result in results if result['state'] != 'done']
    return blocked[0] if blocked else {'state': 'done', 'reason': ''}
