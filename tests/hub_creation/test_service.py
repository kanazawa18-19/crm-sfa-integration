"""非冪等作成の再送・保留・過去分境界を外部I/Oなしで検証する。"""
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.hub_creation.service import HubCreationService
from src.hub_creation.domain import CreationHeld
from src.sync_engine.id_mapping import SQLiteIdMappingStore
from src.sync_engine.sync_event import SyncEvent

NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


class Journal:
    def __init__(self): self.rows = {}; self.claims = []
    def get(self, key, target): return self.rows.get((key, target))
    def has_source(self, key): return any(k[0] == key for k in self.rows)
    def source_for_page(self, page):
        return next((k[0] for k,r in self.rows.items() if k[1] == 'notion' and r.get('externalId') == page), None)
    def reserve(self, key, target, db, fingerprint):
        prior = self.get(key,target)
        if prior and prior['state'] != 'blocked': return False
        if any(r.get('identityHash') == fingerprint and k[1] == target and r['state'] in ('created','reserved') for k,r in self.rows.items()): return False
        self.rows[key,target] = {'state':'reserved', 'identityHash':fingerprint}
        self.claims.append((key,target)); return True
    def hold(self, key, target, db, reason):
        if self.get(key,target) and self.get(key,target)['state'] != 'blocked': return
        self.rows[key,target] = {'state':'blocked','reason':reason}
    def finish(self, key, target, external): self.rows[key,target].update(state='created',externalId=external)
    def resolve_hold(self, key, target, page):
        if self.get(key,target) and self.get(key,target)['state']=='blocked': self.finish(key,target,page)

    def dismiss_hold(self, key, target):
        if self.get(key,target) and self.get(key,target)['state']=='blocked': self.rows.pop((key,target))


class Notion:
    created = '2026-09-28T00:00:00Z'
    title = 'テスト用グループ'
    duplicate = False
    def __init__(self): self.notes=[]; self.creates=0
    def get_raw_page(self, page):
        return {'id':page,'created_time':self.created,'parent':{'database_id':get_schema('chain').notion_database_id},
                'properties':{'グループ名':{'type':'title','title':[{'plain_text':self.title}]}}}
    def query_raw(self, body): return {'results':[{'id':'duplicate'}] if self.duplicate else [], 'has_more':False}
    def upsert_sync_notes(self, page, notes): self.notes.append((page, notes))
    def create_page_once(self, properties): self.creates += 1; return 'created-page'


class Adapter:
    target='zoho'
    def __init__(self): self.creates=0; self.fail=False; self.hold=False
    def plan(self, db, props):
        if self.hold: raise CreationHeld('必須項目が未入力です')
        return dict(props)
    def create(self, db, payload):
        self.creates += 1
        if self.fail: raise TimeoutError('private response')
        return 'external-1'


def setup():
    journal, notion, adapter, store = Journal(), Notion(), Adapter(), SQLiteIdMappingStore()
    service = HubCreationService(store=store,journal=journal,notion_clients={'chain':notion},adapters=[adapter],enabled_since=NOW)
    event=SyncEvent(Tool.NOTION,'chain','page',NOW,properties={'グループ名':notion.title})
    return service,event,journal,notion,adapter,store


def test_old_unmapped_page_is_not_created():
    service,event,journal,notion,adapter,store=setup(); notion.created='2026-09-27T00:00:00Z'
    assert service.handle(event) is None
    assert adapter.creates == 0 and journal.rows == {}


def test_duplicate_is_not_created_or_automatically_linked():
    service,event,journal,notion,adapter,store=setup(); notion.duplicate=True
    assert service.handle(event)=='hub_creation_held'
    assert adapter.creates == 0 and store.get('page') is None
    assert notion.notes


def test_unknown_post_result_is_not_repeated_and_is_visible():
    service,event,journal,notion,adapter,store=setup(); adapter.fail=True
    assert service.handle(event)=='hub_creation_held'
    assert service.handle(event)=='hub_creation_held'
    assert adapter.creates==1
    assert journal.get('notion:page','zoho')['state']=='reserved'
    assert 'private response' not in str(notion.notes)


def test_successful_post_then_mapping_failure_recovers_without_second_post(monkeypatch):
    service,event,journal,notion,adapter,store=setup()
    original=store.upsert
    def fail_after_external(mapping, **kwargs):
        if mapping.zoho_id: raise RuntimeError('保存失敗')
        return original(mapping,**kwargs)
    monkeypatch.setattr(store,'upsert',fail_after_external)
    assert service.handle(event)=='hub_creation_held'
    assert journal.get('notion:page','mapping:zoho')['state']=='blocked'
    monkeypatch.setattr(store,'upsert',original)
    assert service.handle(event)=='hub_creation_complete'
    assert adapter.creates==1 and store.get('page').zoho_id=='external-1'
    assert journal.get('notion:page','mapping:zoho') is None


def test_missing_required_can_be_retried_before_post():
    service,event,journal,notion,adapter,store=setup(); adapter.hold=True
    assert service.handle(event)=='hub_creation_held'; assert adapter.creates==0
    adapter.hold=False
    assert service.handle(event)=='hub_creation_complete'; assert adapter.creates==1


def test_isolation_title_cannot_create_external_records():
    service,event,journal,notion,adapter,store=setup(); notion.title='CRM同期確認_20260927'
    assert service.handle(event)=='hub_creation_held'
    assert adapter.creates==0


def test_new_sheet_registration_retries_without_duplicate_notion():
    service,event,journal,notion,adapter,store=setup()
    class Sheet:
        key='new:registration'; writes=[]
        def read(self,sheet,key,accepted_key=None): return {'グループ名':'新規グループ'}, 12, 9
        def update(self,sheet,key,**kwargs): self.writes.append(kwargs); return 9
    service.sheet_gateway=Sheet()
    event=SyncEvent(Tool.SPREADSHEET,'chain','9',NOW,registration_key='new:registration')
    assert service.handle(event)=='hub_creation_complete'
    assert service.handle(event)=='hub_creation_complete'
    assert notion.creates==1 and adapter.creates==1
    assert store.get('created-page').spreadsheet_row==9


def test_unsupported_files_and_notion_only_properties_do_not_block_source(monkeypatch):
    from dataclasses import replace
    service,event,journal,notion,adapter,store=setup()
    service.notion_clients={'project':notion}
    event=replace(event,db_key='project')
    def raw(page):
        return {'id':page,'created_time':notion.created,
                'parent':{'database_id':get_schema('project').notion_database_id},
                'properties':{'案件名':{'type':'title','title':[{'plain_text':'案件サンプル'}]},
                              '申込書・契約書':{'type':'files','files':[]},
                              '見積書':{'type':'files','files':[{'name':'書類'}]}}}
    monkeypatch.setattr(notion,'get_raw_page',raw)
    # 必須不足の保留ならよいが、files解析の例外でメモも残らない状態にはしない。
    assert service.handle(event) in ('hub_creation_complete','hub_creation_held')
    assert notion.notes


def test_sheet_edit_retries_after_notion_has_received_changes():
    from src.sync_engine.dispatcher import DispatchResult
    service,event,journal,notion,adapter,store=setup();adapter.hold=True
    class Sheet:
        writes=[]
        def read(self,*args,**kwargs): return {'グループ名':'シート新規'},12,9
        def update(self,*args,**kwargs): self.writes.append(kwargs);return 9
    sheet=Sheet();service.sheet_gateway=sheet
    initial=SyncEvent(Tool.SPREADSHEET,'chain','9',NOW,registration_key='new:retry')
    assert service.handle(initial)=='hub_creation_held'
    assert sheet.writes[-1]['notion_key']=='created-page'
    adapter.hold=False
    edited=SyncEvent(Tool.SPREADSHEET,'chain','9',NOW,source_notion_key='created-page')
    # 通常同期が失敗したときは、古いNotion値から外部を作らない。
    service.retry_after_sheet_sync(edited, DispatchResult(skipped=True,reason='stale_event'))
    assert adapter.creates==0
    notion.title='同期後のグループ名'
    service.retry_after_sheet_sync(edited, DispatchResult(skipped=False))
    assert adapter.creates==1
    assert '外部登録完了' in sheet.writes[-1]['status']


def test_not_applicable_is_complete_without_pending_or_false_id():
    from src.hub_creation.domain import CreationNotApplicable
    service,event,journal,notion,adapter,store=setup()
    class NoApp:
        target='kintone'
        def plan(self,*args): raise CreationNotApplicable('対応アプリなし')
    service.adapters=[NoApp()]
    journal.hold('notion:page','kintone','chain','旧保留')
    assert service.handle(event)=='hub_creation_complete'
    assert service.handle(event) is None  # 対象外だけなら次回は通常同期。
    assert journal.get('notion:page','kintone') is None
    assert store.get('page').kintone_id is None
    assert '対象外' in str(notion.notes)


def test_sheet_retry_source_hold_updates_status_with_original_registration():
    from src.sync_engine.dispatcher import DispatchResult
    service,event,journal,notion,adapter,store=setup();adapter.hold=True
    class Sheet:
        def __init__(self): self.writes=[]
        def read(self,*args,**kwargs): return {'グループ名':'シート新規'},12,9
        def update(self,sheet,key,**kwargs): self.writes.append((key,kwargs));return 9
    sheet=Sheet();service.sheet_gateway=sheet
    initial=SyncEvent(Tool.SPREADSHEET,'chain','9',NOW,registration_key='new:retry')
    assert service.handle(initial)=='hub_creation_held'
    notion.title=''
    edited=SyncEvent(Tool.SPREADSHEET,'chain','9',NOW,source_notion_key='created-page')
    service.retry_after_sheet_sync(edited, DispatchResult(skipped=False))
    key,update=sheet.writes[-1]
    assert key=='new:retry'
    assert update['notion_key']=='created-page'
    assert update['status'].startswith('保留: ')
    assert adapter.creates==0


def test_missing_metadata_does_not_fail_again_while_reporting_hold():
    service,event,journal,notion,adapter,store=setup()
    class Sheet:
        def read(self,*args,**kwargs): raise CreationHeld('行を一意に確認できません')
        def update(self,*args,**kwargs): raise CreationHeld('行を一意に確認できません')
    service.sheet_gateway=Sheet()
    event=SyncEvent(Tool.SPREADSHEET,'chain','9',NOW,registration_key='new:missing')
    assert service.handle(event)=='hub_creation_held'
    assert journal.get('sheet:new:missing','source')['state']=='blocked'


def test_completed_destinations_do_not_revalidate_creation_required_fields():
    service,event,journal,notion,adapter,store=setup()
    assert service.handle(event)=='hub_creation_complete'
    notion.title=''
    assert service.handle(event) is None
    assert journal.get('notion:page','source') is None


def test_unmapped_business_value_is_visible_in_source_note(monkeypatch):
    service,event,journal,notion,adapter,store=setup()
    original=notion.get_raw_page
    def raw(page):
        value=original(page)
        value['properties']['その他ブランド']={'type':'rich_text','rich_text':[{'plain_text':'業務サンプル'}]}
        return value
    monkeypatch.setattr(notion,'get_raw_page',raw)
    service.handle(event)
    assert '業務サンプル' in str(notion.notes)


@pytest.mark.parametrize('value', ['2026/09/28', '2026-09-28', 46293])
def test_sheet_creation_date_normalizes_format_and_serial(value):
    from src.hub_creation.domain import sheet_properties
    result=sheet_properties('chain',{'グループ名':'サンプル','最終アプローチ日':value})
    assert result['最終アプローチ日']=='2026-09-28'


def test_disabled_registration_does_not_reach_normal_dispatch():
    from src.sync_engine.production_wiring import SkipTrackingDispatcher
    class Inner:
        def dispatch(self,event): raise AssertionError('通常同期に渡さない')
    event=SyncEvent(Tool.SPREADSHEET,'chain','9',NOW,registration_key='new:disabled')
    result=SkipTrackingDispatcher(Inner()).dispatch(event)
    assert result.skipped and result.reason=='hub_creation_disabled'
