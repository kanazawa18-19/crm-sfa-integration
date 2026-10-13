"""実際の保存先と一意な対応だけを使う更新の回帰試験。"""
from types import SimpleNamespace
import pytest
from src.db_schema.base import SyncScope, Tool
from src.db_schema.project import PROJECT_SCHEMA
from src.sync_engine.sync_targets.zoho_sync import ZohoSyncTarget
from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget

@pytest.mark.parametrize('value,expected', [('連絡済み','連絡済み'),('契約','契約済'),('失注','失注'),('解約','解約'),('アポ',None),('返信なし',None),('',None),([],None)])
def test_status_only_unique_reverse(value,expected):
    target=KintoneSyncTarget(None,'app')
    props={'営業ステータス':value}
    payload=target._to_kintone_payload(props,'project')
    assert payload == ({'ドロップダウン_2':expected} if expected else None)
    assert ('営業ステータス' in target.unsupported_properties(props,db_key='project')) == (expected is None)

@pytest.mark.parametrize('ids,mapping,accepted', [(['p'],SimpleNamespace(db_key='client_master',zoho_id='123'),True), (['p','q'],None,False),([],None,False),('p',None,False),(['p'],None,False),(['p'],SimpleNamespace(db_key='contact',zoho_id='123'),False),(['p'],SimpleNamespace(db_key='client_master',zoho_id=None),False)])
def test_related_id_requires_single_confirmed_target(ids,mapping,accepted):
    target=ZohoSyncTarget(None,'Contacts',get_mapping=lambda _:mapping)
    props={'取引先マスター':ids}
    assert target._to_zoho_payload(props,'contact') == ({'field25':{'id':'123'}} if accepted else None)
    assert ('取引先マスター' in target.unsupported_properties(props,db_key='contact')) != accepted

@pytest.mark.parametrize('value,accepted', [(False,True),(True,True),(0,False),('false',False),(None,False)])
def test_only_actual_booleans(value,accepted):
    target=ZohoSyncTarget(None,'Deals')
    props={'問合せ':value}
    assert target._to_zoho_payload(props,'project') == ({'field48':value} if accepted else None)
    assert ('問合せ' in target.unsupported_properties(props,db_key='project')) != accepted

def test_project_relations_leave_other_fields_absent():
    mappings={'account':SimpleNamespace(db_key='client_master',zoho_id='a'), 'contact':SimpleNamespace(db_key='contact',zoho_id='c')}
    target=ZohoSyncTarget(None,'Deals',get_mapping=mappings.get)
    assert target._to_zoho_payload({'取引先マスター':['account'],'連絡先':['contact']},'project') == {'Account_Name':{'id':'a'},'field10':{'id':'c'}}

def test_dedicated_dates_retain_intended_destination():
    assert PROJECT_SCHEMA.get_property('契約予定日').sync_scope.synced_tools == {Tool.KINTONE,Tool.SPREADSHEET}
    assert PROJECT_SCHEMA.get_property('完了予定日').sync_scope.synced_tools == {Tool.ZOHO,Tool.SPREADSHEET}
    assert PROJECT_SCHEMA.get_property('例外スイッチ（途中解約・複数サービス提案など）').sync_scope == SyncScope.SPREADSHEET_ONLY


def test_wiring_injects_relation_lookup():
    from src.sync_engine.production_wiring import build_zoho_targets_by_db
    lookup=lambda _:SimpleNamespace(db_key='client_master',zoho_id='123')
    target=build_zoho_targets_by_db(object(),get_mapping=lookup)['contact']
    assert target._to_zoho_payload({'取引先マスター':['p']},'contact') == {'field25':{'id':'123'}}

@pytest.mark.parametrize('value,known',[('アポ',True),('返信なし',True),('契約',False),('連絡済み',False)])
def test_status_notification_distinguishes_known_values(value,known):
    from src.sync_engine.production_wiring import SkipTrackingDispatcher
    assert SkipTrackingDispatcher._is_known_gap(Tool.KINTONE,'project','営業ステータス',value) is known

@pytest.mark.parametrize('db,name',[('project','取引先マスター'),('project','連絡先'),('contact','取引先マスター')])
def test_lookup_relations_only_go_to_zoho(db,name):
    from src.db_schema.registry import get_schema
    assert get_schema(db).get_property(name).sync_scope.synced_tools == {Tool.ZOHO}


@pytest.mark.parametrize('value,expected',[('テレアポ','テレアポ'),('オンライン商談','WEB商談'),('問い合わせメール',None),('自動メール',None)])
def test_action_type_exact_mapping(value,expected):
    target=KintoneSyncTarget(None,'app')
    assert target._to_kintone_payload({'アクション種別':value},'action') == ({'actionContent':expected} if expected else None)
