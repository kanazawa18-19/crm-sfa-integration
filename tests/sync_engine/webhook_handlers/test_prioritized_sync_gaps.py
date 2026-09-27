"""実測で利用があった3項目の変換と外向きの項目選択を固定する。"""
import pytest
from src.sync_engine.webhook_handlers.zoho_field_transforms import ZOHO_LABEL_FIELD_MAPPINGS, SKIP_FIELD
from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS, SKIP_FIELD as KINTONE_SKIP_FIELD
from src.sync_engine.outbound_field_mapping import zoho_outbound_field_names, kintone_outbound_field_names

@pytest.mark.parametrize('value,expected', [
    ('2026年9月27日','2026-09-27'),('2026/9/27','2026-09-27'),('2026-09-27','2026-09-27'),
    ('2024年2月29日','2024-02-29'),('',None),(None,None),
    ('来週',SKIP_FIELD),('2026-02-29',SKIP_FIELD),('2026年13月1日',SKIP_FIELD),
    (' ',SKIP_FIELD),(123,SKIP_FIELD),('2026-09-27T10:00:00+09:00',SKIP_FIELD),
])
def test_next_action_date_preserves_existing_on_invalid(value,expected):
    prop,convert=ZOHO_LABEL_FIELD_MAPPINGS['project']['【Notion】次回アクション日']
    assert prop=='次回アクション日'
    assert convert(value)==expected

@pytest.mark.parametrize('value,expected',[('テスト担当','テスト担当'),('',KINTONE_SKIP_FIELD),(None,KINTONE_SKIP_FIELD)])
def test_contact_person_free_text(value,expected):
    prop,convert=KINTONE_FIELD_TRANSFORMS['action']['toPerson']
    assert prop=='先方担当者'
    assert convert(value)==expected

def test_chain_other_and_outbound_targets():
    prop,convert=ZOHO_LABEL_FIELD_MAPPINGS['chain']['その他']
    assert prop=='その他' and convert('テスト')=='テスト'
    assert convert('') is None
    assert zoho_outbound_field_names()['chain']['その他']=='field'
    assert zoho_outbound_field_names()['project']['次回アクション日']=='field31'
    assert kintone_outbound_field_names()['action']['先方担当者']=='toPerson'


def test_new_outbound_fields_require_explicit_property_change():
    from src.sync_engine.webhook_handlers.notion_webhook import _normalize_fetched_page
    from src.db_schema.registry import get_schema
    page={'id':'p','parent':{'database_id':get_schema('project').notion_database_id},'last_edited_time':'2026-09-27T00:00:00Z',
          'properties':{'次回アクション日':{'id':'date','type':'date','date':None},'メモ':{'id':'note'}}}
    assert '次回アクション日' not in _normalize_fetched_page(page)['properties']
    assert '次回アクション日' not in _normalize_fetched_page(page,['unknown'])['properties']
    assert '次回アクション日' in _normalize_fetched_page(page,['date'])['properties']
    assert '次回アクション日' not in _normalize_fetched_page(page,['note'])['properties']


def test_invalid_date_is_not_emitted_by_webhook_handler():
    from src.sync_engine.webhook_handlers.zoho_webhook import zoho_payload_to_sync_events
    payload={'module':'Deals','ids':['123'],'server_time':1720000000000,
             'affected_values':[{'record_id':'123','values':{'field31':'来週'}}]}
    events=zoho_payload_to_sync_events(payload,{},module_to_db_key={'Deals':'project'})
    assert '次回アクション日' not in events[0].properties
