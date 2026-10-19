"""通常同期とは別のNotion接続で原本を読み、共通配送を明示実行する。"""
import os
from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.email_review.gateway import Gateway
from src.email_review.domain import Conflict
from src.email_review.repository import Repository as Reviews
from src.contact_name_job.worker import NameOnlyClient
from src.contact_affiliation_job.evidence import mail_evidence, company_lines, zoho_evidence


class AffiliationClient(NameOnlyClient):
    def get_raw_page_with_relations(self, page_id, property_names):
        # 姓名用とは異なり、所属を完全に読む。大きい活動関連は判断に不要。
        from src.sync_engine.clients.notion_client import HttpNotionClient
        return HttpNotionClient.get_raw_page_with_relations(self, page_id, {'取引先マスター'})


class JobGateway(Gateway):
    def client(self, key):
        api_key = os.environ['CONTACT_AFFILIATION_NOTION_API_KEY']
        if not api_key or api_key == os.environ.get('NOTION_API_KEY'):
            raise ValueError('通常同期とは別のNotion接続が必要です')
        return AffiliationClient(key, get_schema(key).notion_database_id,
            api_key=api_key,
            timeout=15, max_retries=0, max_rate_limit_retries=0)

    def discover(self, cursor):
        body = {'page_size': 100, 'sorts': [{'timestamp': 'created_time', 'direction': 'ascending'}]}
        if cursor.get('watermark'):
            body['filter'] = {'timestamp': 'created_time', 'created_time': {'on_or_after': cursor['watermark']}}
        if cursor.get('cursor'):
            body['start_cursor'] = cursor['cursor']
        data = self.client('contact').query_raw(body)
        pages = data.get('results') or []
        count = cursor.get('count', 0) + len(pages)
        truncated = (data.get('request_status') or {}).get('type') == 'incomplete'
        if count >= 9000 or truncated:
            watermark = pages[-1]['created_time'] if pages else cursor.get('watermark')
            if not watermark or watermark == cursor.get('watermark'):
                raise ValueError('同時刻の境界が上限に達しました。巡回を確認してください')
            after = {'watermark': watermark, 'count': 0}
        elif data.get('has_more'):
            if not data.get('next_cursor'):
                raise ValueError('巡回カーソルがありません')
            after = {**cursor, 'cursor': data['next_cursor'], 'count': count}
        else:
            after = {}  # 次周で新規・未解決を再確認する。
        return pages, after

    def collect(self, key, email, repository):
        contact = self.live('contact', key)
        raw = mail_evidence(repository.mail_rows(email), email, [contact.get('名前'),
            str(contact.get('姓') or '') + str(contact.get('名') or '')])
        from src.sync_engine.production_wiring import get_production_wiring
        wiring = get_production_wiring()
        mapping = wiring.id_mapping_store.get(key)
        zoho = []
        if mapping and mapping.db_key == 'contact' and mapping.zoho_id:
            dispatcher = getattr(wiring.dispatcher, '_dispatcher', wiring.dispatcher)
            record = dispatcher._targets[Tool.ZOHO].get_record(str(mapping.zoho_id), db_key='contact')
            if not record or str(record.get('id')) != str(mapping.zoho_id):
                raise Conflict('Zoho原本のIDを確認できません')
            if str(record.get('Email') or '').strip().casefold() != email:
                raise Conflict('Zoho原本の本人メールが一致しません')
            lookup = record.get('field25') or {}
            resolved = wiring.id_mapping_store.find_by_external_id(Tool.ZOHO, str(lookup['id']), db_key='client_master') if lookup.get('id') else None
            company_id = None
            if resolved and resolved.db_key == 'client_master' and str(resolved.zoho_id) == str(lookup['id']):
                self.live('client_master', resolved.notion_key)
                company_id = resolved.notion_key
            zoho = zoho_evidence(record, email, company_id)
        extra = repository.extra_evidence(email)
        masters = Reviews().masters([r['company_name'] for r in raw + zoho + extra])
        # Zohoの明示社名はマスター未登録でも矛盾の証拠として残す。
        return company_lines(raw, masters) + zoho + extra, masters

    def dispatch(self, key, page_id, properties, *, expected_email):
        from src.sync_engine.production_wiring import get_production_wiring
        wiring = get_production_wiring()
        dispatcher = getattr(wiring.dispatcher, '_dispatcher', wiring.dispatcher)
        mapping = wiring.id_mapping_store.get(page_id)
        parent = wiring.id_mapping_store.get(properties['取引先マスター'][0])
        if not mapping or not parent or not mapping.zoho_id or not parent.zoho_id:
            return 'partial'
        target = dispatcher._targets[Tool.ZOHO]
        def read():
            return target.get_record(str(mapping.zoho_id), db_key='contact')
        record = read()
        current = self.live('contact', page_id)
        if (not record or str(record.get('id')) != str(mapping.zoho_id)
                or str(record.get('Email') or '').strip().casefold() != str(current.get('メールアドレス') or '').strip().casefold()):
            raise Conflict('配送先の本人を確認できません')
        if (str(current.get('メールアドレス') or '').strip().casefold() != expected_email
                or current.get('取引先マスター') != properties['取引先マスター']):
            raise Conflict('配送前に本人または所属が変わりました')
        existing = record.get('field25') or {}
        if existing and str(existing.get('id')) != str(parent.zoho_id):
            raise Conflict('配送先に別の所属があります')
        from datetime import datetime
        from src.sync_engine.sync_event import SyncEvent
        event = SyncEvent(source_tool=Tool.NOTION, db_key=key, external_id=page_id,
            occurred_at=datetime.fromisoformat(current['_last_edited_time'].replace('Z', '+00:00')),
            properties=properties, expected_source_properties={**properties, 'メールアドレス': current.get('メールアドレス')})
        result = wiring.dispatcher.dispatch(event)
        if result.skipped and result.reason == 'source_changed_after_handoff':
            return 'partial'
        saved = read()
        company = target.get_record(str(parent.zoho_id), db_key='client_master')
        current = self.live('contact', page_id)
        if (str((saved or {}).get('id')) == str(mapping.zoho_id)
                and str(((saved or {}).get('field25') or {}).get('id')) == str(parent.zoho_id)
                and str((company or {}).get('id')) == str(parent.zoho_id)
                and current.get('取引先マスター') == properties['取引先マスター']
                and str(current.get('メールアドレス') or '').strip().casefold() == expected_email
                and str((saved or {}).get('Email') or '').strip().casefold() == expected_email):
            return 'delivered'
        return 'partial'
