"""9経路が必要値を持つ場合に計画でき、不足や候補を保留する。外部書込みなし。"""
import json
from types import SimpleNamespace
import pytest
from src.hub_creation.adapters import ZohoCreationAdapter, KintoneCreationAdapter


def response(data):
    return SimpleNamespace(ok=True, status_code=200, json=lambda: data)


@pytest.mark.parametrize('db,properties,required', [
    ('chain', {'グループ名':'検証','施設数':'0','本社':'東京','本社所在地':'東京','運営会社':'検証'},
     {'Name':'text','field2':'integer','field5':'text','field14':'text','field20':'text'}),
    ('client_master', {'取引先名':'検証','住所':'東京','郵便番号':'0000000','都道府県':'東京都','顧客種別':'宿泊施設'},
     {'Account_Name':'text','field4':'text','field11':'text','field13':'text','field14':'picklist'}),
    ('contact', {'名前':'検証 姓名','姓':'検証','名':'姓名','メールアドレス':'qa@example.invalid','取引先マスター':['client']},
     {'Last_Name':'text','First_Name':'text','Email':'email','field25':'lookup'}),
    ('project', {'案件名':'検証案件','完了予定日':'2026-10-01','営業ステータス':'商談中（A）','新規・既存種別':'新規','作成日':'2026-09-28','リードソース1':['テレアポ'],'取引先マスター':['client'],'連絡先':['contact']},
     {'Deal_Name':'text','Closing_Date':'date','field5':'picklist','field42':'date','field65':'multiselectpicklist','Account_Name':'lookup','field10':'lookup'}),
    ('product', {'名前':'検証商品','商品カテゴリー':'メイリー','標準初期費用':0,'標準月額費用':0,'先方担当者':['contact'],'案件管理':['project']},
     {'Product_Name':'text','Product_Category':'picklist','field':'integer','field1':'integer','field7':'lookup','field12':'lookup'}),
    ('action', {'商談回数・電話回数・メール回数（何回目）':'検証連絡','履歴メモ':'検証','先方担当者':'担当','アクション日':'2026-09-28','👨‍👩‍👧‍👦 取引先マスター':['client']},
     {'Name':'text','field':'textarea','field2':'text','field4':'date','field6':'lookup'}),
])
def test_zoho_six_creation_routes(db, properties, required):
    # 任意項目を混ぜず、実必須の対応とlookupのID解決を確認する。
    fields = [{'api_name':code,'data_type':kind,'system_mandatory':True,
               'pick_list_values':[{'actual_value':'テレアポ'}]} for code,kind in required.items()]
    class Client:
        def _request(self, method, path):
            if '/settings/fields?' in path:
                return response({'fields':fields})
            if '/settings/layouts?' in path:
                return response({'layouts':[{'sections':[{'fields':[]}]}]})
            return response({'data':[], 'info':{'more_records':False}})
    store = SimpleNamespace(get=lambda key: SimpleNamespace(db_key={'client':'client_master','contact':'contact','project':'project'}[key], zoho_id='123'))
    adapter = ZohoCreationAdapter(Client(), store)
    # 任意項目も型確認するため、マッピング済み項目の実型を補う。
    from src.sync_engine.outbound_field_mapping import translate_properties, zoho_outbound_field_names
    from src.sync_engine.outbound_value_mapping import translate_choice_value
    mapped, _ = translate_properties(zoho_outbound_field_names(), db, properties, translate_choice_value)
    for code, value in mapped.items():
        if code not in required and code != 'Full_Name':
            fields.append({'api_name':code,'data_type':'text'})
    result = adapter.plan(db, properties)
    assert set(required) <= result.keys()
    if db == 'project':
        assert result['Closing_Date'] == '2026-10-01'
    if db == 'client_master':
        assert result['field4'] == properties['取引先名']


@pytest.mark.parametrize('db,properties,fields', [
    ('client_master', {'取引先名':'検証'}, {'顧客名':{'type':'SINGLE_LINE_TEXT','unique':True,'required':True}}),
    ('project', {'案件名':'検証','作成日':'2026-09-28','契約予定日':'2026-10-01','担当メンバー':['user']},
     {'店舗名':{'type':'SINGLE_LINE_TEXT','required':True},'日付_0':{'type':'DATE','required':True},'日付':{'type':'DATE','required':True},'営業担当者':{'type':'USER_SELECT','required':True}}),
    ('action', {'履歴メモ':'検証','担当営業':['user'],'提案サービス':['メイリー'],'👨‍👩‍👧‍👦 取引先マスター':['client']},
     {'comment':{'type':'MULTI_LINE_TEXT','required':True},'cnctorMember':{'type':'USER_SELECT','required':True},'service':{'type':'MULTI_SELECT','required':True,'options':{'メイリー':{}}},'client_name':{'type':'SINGLE_LINE_TEXT','required':True}}),
])
def test_kintone_three_creation_routes(db, properties, fields, monkeypatch):
    from src.hub_creation import adapters
    monkeypatch.setenv('CRM_USER_MAPPING_JSON', json.dumps({'verified':True,'users':[{'notion_id':'user','kintone_code':'member','enabled':True}]}))
    def request(method, url, **kw):
        return response({'properties':fields} if 'fields.json' in url else {'records':[]})
    monkeypatch.setattr(adapters, 'request_with_retry', request)
    client = SimpleNamespace(_domain='example.invalid', _headers=lambda **kw:{},_timeout=1,_max_retries=0,_backoff_base=0,
                             get_record=lambda *a:{'顧客名':'検証取引先'})
    targets = {key:SimpleNamespace(_client=client,_app='1') for key in ('client_master','project','action')}
    store = SimpleNamespace(get=lambda _:SimpleNamespace(db_key='client_master', kintone_id='1'))
    result = KintoneCreationAdapter(targets, store).plan(db, properties)
    assert set(fields) <= result.keys()


def test_contact_matching_email_holds_even_when_name_differs():
    from src.hub_creation.domain import CreationHeld
    class Client:
        def _request(self, method, path):
            return response({'data':[{'id':'123','Last_Name':'別','First_Name':'氏名','Email':'QA@example.invalid'}], 'info':{'more_records':False}})
    with pytest.raises(CreationHeld, match='メール'):
        ZohoCreationAdapter(Client())._check_contact_duplicates('Contacts', {'姓':'検証','名':'姓名','メールアドレス':'qa@example.invalid'})


def test_contact_empty_given_name_representations_do_not_bypass_duplicates():
    from src.hub_creation.domain import CreationHeld
    class Client:
        def _request(self, method, path):
            return response({'data':[{'id':'123','Last_Name':'同姓','First_Name':None}], 'info':{'more_records':False}})
    with pytest.raises(CreationHeld, match='姓名'):
        ZohoCreationAdapter(Client())._check_contact_duplicates('Contacts', {'姓':'同姓','名':''})


def test_zoho_creation_preserves_all_controller_choices_and_memo():
    from src.sync_engine.decided_choices import controller_from_external
    class Client:
        def _request(self, method, path):
            if '/settings/fields?' in path:
                return response({'fields': [{'api_name': code, 'data_type': kind}
                    for code, kind in [('Deal_Name', 'text'), ('field20', 'picklist'), ('field70', 'textarea')]]})
            if '/settings/layouts?' in path:
                return response({'layouts': [{'sections': [{'fields': []}]}]})
            return response({'data': [], 'info': {'more_records': False}})
    result = ZohoCreationAdapter(Client()).plan('project', {
        '案件名': '検証', 'メモ': '入力本文', 'サイトコントローラー': ['リンカーン', 'ねっぱん']})
    assert result['field70'].startswith('入力本文')
    assert controller_from_external(result['field20'], result['field70']) == ['リンカーン', 'ねっぱん']


def test_action_unfinished_owner_rollup_does_not_use_fallback(monkeypatch):
    from src.hub_creation import adapters
    from src.hub_creation.domain import CreationHeld
    monkeypatch.setenv('CRM_USER_MAPPING_JSON', json.dumps({'verified': True, 'fallback_notion_id': 'user',
        'users': [{'notion_id': 'user', 'kintone_code': 'member', 'enabled': True}]}))
    monkeypatch.setattr(adapters, 'request_with_retry', lambda *args, **kwargs: response({
        'properties': {'cnctorMember': {'type': 'USER_SELECT', 'required': True}}}))
    client = SimpleNamespace(_domain='example.invalid', _headers=lambda **kw: {},
        _timeout=1, _max_retries=0, _backoff_base=0)
    adapter = KintoneCreationAdapter({'action': SimpleNamespace(_client=client, _app='1')})
    with pytest.raises(CreationHeld, match='集計が未確定'):
        adapter.plan('action', {'提案サービス': ['メイリー']})


def test_unverified_owner_directory_holds_even_optional_owner(monkeypatch):
    from src.hub_creation import adapters
    from src.hub_creation.domain import CreationHeld
    monkeypatch.delenv('CRM_USER_MAPPING_JSON', raising=False)
    monkeypatch.setattr(adapters, 'request_with_retry', lambda *args, **kwargs: response({
        'properties': {'営業担当者': {'type': 'USER_SELECT', 'required': False}}}))
    client = SimpleNamespace(_domain='example.invalid', _headers=lambda **kw: {},
        _timeout=1, _max_retries=0, _backoff_base=0)
    adapter = KintoneCreationAdapter({'project': SimpleNamespace(_client=client, _app='1')})
    with pytest.raises(CreationHeld, match='担当者対応の設定'):
        adapter.plan('project', {'担当メンバー': ['user']})


def test_zoho_optional_unmapped_single_owner_is_omitted_after_verified_lookup(monkeypatch):
    monkeypatch.setenv('CRM_USER_MAPPING_JSON', json.dumps({'verified': True,
        'users': [{'notion_id': 'known', 'zoho_id': '123', 'enabled': True}]}))
    class Client:
        def _request(self, method, path):
            if '/settings/fields?' in path:
                return response({'fields': [{'api_name': 'Name', 'data_type': 'text'},
                    {'api_name': 'Owner', 'data_type': 'ownerlookup', 'system_mandatory': False}]})
            if '/settings/layouts?' in path:
                return response({'layouts': [{'sections': [{'fields': []}]}]})
            return response({'data': [], 'info': {'more_records': False}})
    assert ZohoCreationAdapter(Client()).plan('chain', {'グループ名': '検証', '担当': ['unknown']}) == {'Name': '検証'}
    monkeypatch.delenv('CRM_USER_MAPPING_JSON')
    from src.hub_creation.domain import CreationHeld
    with pytest.raises(CreationHeld, match='担当者対応の設定'):
        ZohoCreationAdapter(Client()).plan('chain', {'グループ名': '検証', '担当': ['unknown']})
