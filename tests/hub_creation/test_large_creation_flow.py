"""実サービス配線と同じアダプターで、照合再開から単発作成まで通す。"""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from tests.hub_creation.test_service import setup
from tests.hub_creation.test_adapters import Client as MetadataClient
from tests.hub_creation.test_duplicate_scan import Client as ScanClient, Journal
from src.hub_creation.adapters import ZohoCreationAdapter
from src.hub_creation.duplicate_scan import ZohoDuplicateScan
from src.infrastructure.http_budget import HttpBudgetExceeded


class Client(ScanClient, MetadataClient):
    def __init__(self):
        ScanClient.__init__(self,4001)
        self.posts=0; self.unknown=False; self.list_timeout=False
    def _request(self,method,path):
        if '/settings/' in path: return MetadataClient._request(self,method,path)
        if self.list_timeout: raise HttpBudgetExceeded()
        page=int(parse_qs(urlsplit(path).query)['page'][0])
        data={'data':[{'id':str(page),'Name':'別名'}],'info':{'more_records':True}}
        return SimpleNamespace(ok=True,status_code=200,json=lambda:data)
    def insert_record(self,module,payload):
        self.posts+=1
        if self.unknown: raise TimeoutError('不明応答')
        return 'external-created'


def wiring():
    service,event,attempts,notion,_,store=setup()
    client=Client(); scans=Journal()
    service.adapters=[ZohoCreationAdapter(client,store,ZohoDuplicateScan(client,scans))]
    return service,event,attempts,notion,client,scans,store


def test_large_creation_resumes_then_creates_exactly_once():
    service,event,attempts,notion,client,scans,store=wiring()
    client.interrupt=2
    assert service.handle(event)=='hub_creation_held'
    assert client.posts==0 and scans.row['checkpoint']['last_id']=='2000'
    client.interrupt=None
    assert service.handle(event)=='hub_creation_complete'
    assert client.posts==1 and store.get('page').zoho_id=='external-created'
    service.handle(event)
    assert client.posts==1


def test_unknown_post_after_completed_scan_remains_reserved():
    service,event,attempts,notion,client,scans,store=wiring()
    client.unknown=True
    assert service.handle(event)=='hub_creation_held'
    assert attempts.get('notion:page','zoho')['state']=='reserved'
    assert service.handle(event)=='hub_creation_held'
    assert client.posts==1


def test_short_list_timeout_switches_to_durable_scan():
    service,event,attempts,notion,client,scans,store=wiring()
    client.list_timeout=True
    assert service.handle(event)=='hub_creation_held'
    assert scans.row['checkpoint']['last_id']=='0'
    assert service.handle(event)=='hub_creation_complete'
    assert client.posts==1


def test_edit_during_scan_prevents_post_with_old_input():
    service,event,attempts,notion,client,scans,store=wiring()
    request=client.request
    def change(*a,**kw):
        result=request(*a,**kw)
        notion.title='走査中の変更'
        return result
    client.request=change
    assert service.handle(event)=='hub_creation_held'
    assert client.posts==0
    assert '照合中に登録元が変更' in attempts.get('notion:page','zoho')['reason']
