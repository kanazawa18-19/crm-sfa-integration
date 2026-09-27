from types import SimpleNamespace
import pytest
from src.hub_creation.adapters import ZohoCreationAdapter
from src.hub_creation.domain import CreationHeld


class Client:
    search_status=200
    result={'data':[],'info':{'more_records':False}}
    required='Name'
    def _request(self, method, path):
        if '/settings/fields?' in path: data={'fields':[{'api_name':'Name','system_mandatory':True,'data_type':'text'}]}; status=200
        elif '/settings/layouts?' in path: data={'layouts':[{'sections':[{'fields':[{'api_name':self.required,'required':True}]}]}]}; status=200
        else: data=self.result;status=self.search_status
        return SimpleNamespace(ok=True,status_code=status,json=lambda:data)


def test_layout_required_field_is_checked():
    client=Client();client.required='AdditionalRequired'
    with pytest.raises(CreationHeld,match='必須'):
        ZohoCreationAdapter(client).plan('chain',{'グループ名':'サンプル'})


def test_duplicate_stops_creation():
    client=Client();client.search_status=200;client.result={'data':[{'id':'existing','Name':'サンプル'}],'info':{'more_records':False}}
    with pytest.raises(CreationHeld,match='同名'):
        ZohoCreationAdapter(client).plan('chain',{'グループ名':'サンプル'})


def test_malformed_search_result_is_not_treated_as_no_duplicates():
    client=Client();client.result={}
    with pytest.raises(CreationHeld,match='結果を確認'):
        ZohoCreationAdapter(client).plan('chain',{'グループ名':'サンプル'})


def test_verified_empty_search_allows_plan():
    assert ZohoCreationAdapter(Client()).plan('chain',{'グループ名':'サンプル'})['Name']=='サンプル'


def test_kintone_respects_verified_radio_default(monkeypatch):
    from src.hub_creation import adapters
    client=SimpleNamespace(_domain='example.test',_headers=lambda **kw:{},_timeout=1,_max_retries=0,_backoff_base=0)
    target=SimpleNamespace(_client=client,_app='1')
    fields={'顧客名':{'required':True,'unique':True,'type':'SINGLE_LINE_TEXT'},
            'ラジオボタン':{'required':True,'type':'RADIO_BUTTON','defaultValue':'未契約','options':{'未契約':{},'契約済':{}}}}
    def request(method,url,**kw):
        data={'properties':fields} if 'fields.json' in url else {'records':[]}
        return SimpleNamespace(ok=True,status_code=200,json=lambda:data)
    monkeypatch.setattr(adapters,'request_with_retry',request)
    result=adapters.KintoneCreationAdapter({'client_master':target}).plan('client_master',{'取引先名':'サンプル'})
    assert result['ラジオボタン']=='未契約'
    with pytest.raises(CreationHeld,match='顧客種別の選択肢'):
        adapters.KintoneCreationAdapter({'client_master':target}).plan('client_master',{'取引先名':'サンプル','顧客種別':'未対応'})
    fields['ラジオボタン']['defaultValue']='不明な既定値'
    with pytest.raises(CreationHeld,match='ラジオボタン.*対応未実装'):
        adapters.KintoneCreationAdapter({'client_master':target}).plan('client_master',{'取引先名':'サンプル'})


def test_missing_required_reason_names_input_and_unimplemented_fields():
    from src.hub_creation.adapters import missing_required_reason
    result=missing_required_reason({'Name','Pipeline'},{},{},{'名前':'Name'},{})
    assert 'Name: 入力が必要（Notion: 名前）' in result
    assert 'Pipeline: 対応未実装' in result


@pytest.mark.parametrize('value,expected', [('12',12),(12,12),(12.0,12),('0',0)])
def test_integer_creation_field_is_normalized(value, expected):
    from src.hub_creation.adapters import normalize_creation_payload
    assert normalize_creation_payload({'field2':value},{'field2':{'data_type':'integer'}})=={'field2':expected}


@pytest.mark.parametrize('value', ['12施設',True,float('nan'),float('inf'),'NaN','Infinity',12.5,{},[]])
def test_ambiguous_or_nonfinite_integer_is_held(value):
    from src.hub_creation.adapters import normalize_creation_payload
    with pytest.raises(CreationHeld,match='field2'):
        normalize_creation_payload({'field2':value},{'field2':{'data_type':'integer'}})


@pytest.mark.parametrize('kintone,kind', [(False,'currency'),(True,'NUMBER')])
def test_scalar_numeric_types_validate_before_creation(kintone,kind):
    from src.hub_creation.adapters import normalize_creation_payload
    fields={'amount':{('type' if kintone else 'data_type'):kind}}
    assert normalize_creation_payload({'amount':'12.5'},fields,kintone=kintone)=={'amount':12.5}
    for value in [True,float('inf'),'invalid']:
        with pytest.raises(CreationHeld):
            normalize_creation_payload({'amount':value},fields,kintone=kintone)


def test_unverified_complex_type_is_held():
    from src.hub_creation.adapters import normalize_creation_payload
    with pytest.raises(CreationHeld,match='未対応の型'):
        normalize_creation_payload({'owner':{'id':'123'}},{'owner':{'data_type':'lookup'}})


def test_chain_integer_type_is_checked_before_duplicate_search():
    class TypedClient(Client):
        def _request(self,method,path):
            if '/settings/fields?' in path:
                data={'fields':[{'api_name':'Name','data_type':'text','system_mandatory':True},
                                {'api_name':'field2','data_type':'integer','system_mandatory':True}]}
                return SimpleNamespace(ok=True,status_code=200,json=lambda:data)
            return super()._request(method,path)
    adapter=ZohoCreationAdapter(TypedClient())
    assert adapter.plan('chain',{'グループ名':'サンプル','施設数':'12'})['field2']==12
    with pytest.raises(CreationHeld,match='field2'):
        adapter.plan('chain',{'グループ名':'サンプル','施設数':'12施設'})


def test_record_list_checks_later_pages_without_search():
    class Paged(Client):
        def _request(self, method, path):
            assert '/search' not in path
            if '/settings/' in path: return super()._request(method,path)
            from urllib.parse import parse_qs,urlsplit
            page=int(parse_qs(urlsplit(path).query)['page'][0])
            data={'data':[{'id':str(page),'Name':'別名' if page==1 else 'サンプル'}],
                  'info':{'more_records':page==1}}
            return SimpleNamespace(ok=True,status_code=200,json=lambda:data)
    with pytest.raises(CreationHeld,match='同名'):
        ZohoCreationAdapter(Paged()).plan('chain',{'グループ名':'サンプル'})


def test_record_list_limit_holds_without_post():
    class Paged(Client):
        count=0
        def _request(self,method,path):
            if '/settings/' in path:return super()._request(method,path)
            self.count+=1
            data={'data':[{'id':str(self.count),'Name':'別名'}],'info':{'more_records':True}}
            return SimpleNamespace(ok=True,status_code=200,json=lambda:data)
    client=Paged()
    with pytest.raises(CreationHeld,match='2000件'):
        ZohoCreationAdapter(client).plan('chain',{'グループ名':'サンプル'})
    assert client.count==10


@pytest.mark.parametrize('result', [{}, {'data':[],'info':{}}, {'data':[],'info':{'more_records':True}},
                                   {'data':[{'id':'1'}],'info':{'more_records':False}}])
def test_partial_or_malformed_list_holds(result):
    client=Client();client.result=result
    with pytest.raises(CreationHeld):
        ZohoCreationAdapter(client).plan('chain',{'グループ名':'サンプル'})


def test_record_list_network_failure_holds_with_fixed_reason():
    class Broken(Client):
        def _request(self,method,path):
            if '/settings/' in path:return super()._request(method,path)
            raise TimeoutError('private response')
    with pytest.raises(CreationHeld,match='最後まで取得') as error:
        ZohoCreationAdapter(Broken()).plan('chain',{'グループ名':'サンプル'})
    assert 'private' not in str(error.value)
