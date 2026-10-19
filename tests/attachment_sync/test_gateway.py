"""実GatewayのHTTP境界と、不確定な保存後の再読を検証する。"""
import hashlib
from copy import deepcopy
from dataclasses import replace
from contextlib import contextmanager
import pytest
from src.attachment_sync.gateway import Gateway
from src.attachment_sync.domain import Held, MAX_BYTES
from src.attachment_sync import http
from src.attachment_sync.worker import process
from tests.attachment_sync.test_sync import S, FakeGateway, Repo, row, lock


class Response:
    ok=True
    status_code=200
    def __init__(self,data,status=200):self.data=data;self.status_code=status
    def json(self):return self.data


def test_drive_multipart_related_id_and_content_verification(monkeypatch):
    g=Gateway.__new__(Gateway);g.folder='folder';g.google=lambda:{}
    asset={'id':'asset','name':'file.pdf','drive_id':'reserved'}
    saved={'id':'reserved','size':'4','md5Checksum':hashlib.md5(b'file').hexdigest(),'parents':['folder'],'appProperties':{'crmAttachment':'asset'}}
    calls=[]
    def request(method,url,**kw):
        calls.append((method,url,kw))
        if len(calls)==1:return Response({},404)
        if method=='POST':
            assert kw['headers']['Content-Type'].startswith('multipart/related; boundary=')
            assert b'"id": "reserved"' in kw['data'] and b'file\r\n--' in kw['data']
            return Response({})
        return Response(saved)
    monkeypatch.setattr(http,'request',request)
    assert g.archive(asset,b'file',lambda:None,allow_create=True).endswith('/reserved/view')
    assert len([c for c in calls if c[0]=='POST'])==1
    saved['md5Checksum']='changed'
    with pytest.raises(Held):g.archive(asset,b'file',lambda:None)


def test_drive_uncertain_creation_does_not_recreate(monkeypatch):
    g=Gateway.__new__(Gateway);g.folder='folder';g.google=lambda:{}
    monkeypatch.setattr(http,'request',lambda method,*a,**k:Response({},404) if method=='GET' else pytest.fail('再作成しました'))
    with pytest.raises(Held):g.archive({'drive_id':'reserved'},b'file',lambda:None)


@pytest.mark.parametrize('uncertain', [False,True])
def test_kintone_preserves_existing_files_and_recovers_put(uncertain,monkeypatch):
    g=Gateway.__new__(Gateway);g.config={'action':{'kintone_field':'files'}};g.google=lambda:{}
    monkeypatch.setenv('KINTONE_APP_ID_ACTION','1')
    asset={'id':'a'*64,'name':'quote.pdf','drive_id':'reserved','sha256':hashlib.sha256(b'file').hexdigest()}
    raw={'$revision':{'value':'7'},'files':{'type':'FILE','value':[{'fileKey':'existing','name':'old.pdf'}]}}
    g.krecord=lambda *a:deepcopy(raw)
    g.kdownload=lambda *a:b'file'
    monkeypatch.setattr(http,'download',lambda *a,**k:b'file')
    writes=[];receipt={}
    def checkpoint(value):receipt.clear();receipt.update(value)
    def kintone(db,path,method,**kw):
        writes.append((method,path))
        if method=='POST':
            assert kw['files']['file'][1]==b'file'
            assert kw['files']['file'][0].endswith('.pdf')
            raw['new_name']=kw['files']['file'][0]
            return Response({'fileKey':'uploaded'})
        payload=kw['json'];assert payload['revision']=='7'
        assert payload['record']['files']['value']==[{'fileKey':'existing'},{'fileKey':'uploaded'}]
        raw['files']['value'].append({'name':raw['new_name'],'fileKey':'persisted'})
        if uncertain:raise TimeoutError()
        return Response({})
    g.kintone=kintone
    if uncertain:
        with pytest.raises(TimeoutError):g.deliver_kintone(S,asset,'record',receipt,checkpoint,lambda:None)
    else:
        assert g.deliver_kintone(S,asset,'record',receipt,checkpoint,lambda:None)['state']=='verified'
    assert g.deliver_kintone(S,asset,'record',receipt,checkpoint,lambda:None)['id']=='persisted'
    assert writes==[('POST','file.json'),('PUT','record.json')]


def test_kintone_conflict_then_manual_delete_does_not_retry_put(monkeypatch):
    g=Gateway.__new__(Gateway);g.config={'action':{'kintone_field':'files'}}
    g.krecord=lambda *a:{'files':{'type':'FILE','value':[]}}
    with pytest.raises(Held):g.deliver_kintone(S,{'id':'a'*64,'name':'file.pdf'},'r',{'state':'writing'},lambda _:None,lambda:None)


def test_lost_mail_row_lock_prevents_write():
    class Gateway(FakeGateway):
        @contextmanager
        def source_guard(self, source):
            def lost():raise ConnectionError()
            yield lost
    repo=Repo();g=Gateway()
    assert process(repo,g,row(repo),lock)=='retry'
    assert g.creates==0 and not g.writes


def test_large_attachment_held_without_affecting_small():
    repo=Repo();g=FakeGateway()
    from dataclasses import asdict
    large=replace(S,file_id='large',size=MAX_BYTES+1)
    assert process(repo,g,{'data':asdict(large)},lock)=='held'
    assert process(repo,g,row(repo),lock)=='verified'


def test_notion_uses_real_client_signature(monkeypatch):
    # メソッドの受理引数を実クラスから取得し、paramsなど契約外引数を拒否する。
    from unittest.mock import create_autospec
    from src.sync_engine.clients.notion_client import HttpNotionClient
    client=create_autospec(HttpNotionClient,instance=True)
    client._request.return_value=Response({'results':[]})
    g=Gateway.__new__(Gateway);g.page=lambda *a:{};g.notion=lambda db:client
    with pytest.raises(Held):g.deliver_notion(S,{'url':'https://drive.google.com/file/d/f/view','name':'f'}, {},lambda _:None,lambda:None)
    assert client._request.call_args_list[0].args[1].endswith('?page_size=100')


def test_kintone_revision_conflict_stays_held_without_second_upload(monkeypatch):
    g=Gateway.__new__(Gateway);g.config={'action':{'kintone_field':'files'}};g.google=lambda:{}
    monkeypatch.setenv('KINTONE_APP_ID_ACTION','1')
    g.krecord=lambda *a:{'$revision':{'value':'7'},'files':{'type':'FILE','value':[{'fileKey':'existing','name':'old.pdf'}]}}
    monkeypatch.setattr(http,'download',lambda *a,**k:b'file')
    asset={'id':'a'*64,'name':'f.pdf','drive_id':'reserved','sha256':hashlib.sha256(b'file').hexdigest()}
    calls=[];receipt={}
    def checkpoint(value):receipt.clear();receipt.update(value)
    def request(db,path,method,**kwargs):
        calls.append(method)
        if method=='POST':return Response({'fileKey':'uploaded'})
        assert kwargs['json']['revision']=='7'
        raise http.RemoteError('添付API HTTP 409')
    g.kintone=request
    with pytest.raises(http.RemoteError):g.deliver_kintone(S,asset,'record',receipt,checkpoint,lambda:None)
    with pytest.raises(Held):g.deliver_kintone(S,asset,'record',receipt,checkpoint,lambda:None)
    assert calls==['POST','PUT']


def test_read_failure_leaves_drive_reservation_retryable(monkeypatch):
    g=Gateway.__new__(Gateway);g.folder='folder';g.google=lambda:{}
    asset={'id':'asset','name':'f','drive_id':'reserved','phase':'reserved'}
    def mark():asset['phase']='writing'
    monkeypatch.setattr(http,'request',lambda *a,**kw:(_ for _ in ()).throw(TimeoutError()))
    with pytest.raises(TimeoutError):g.archive(asset,b'file',lambda:None,allow_create=True,before_create=mark)
    assert asset['phase']=='reserved'


def test_missing_target_not_reserved_then_mapping_can_appear():
    from types import SimpleNamespace
    g=Gateway.__new__(Gateway)
    mapping=SimpleNamespace(zoho_id=None,kintone_id=None)
    g.mapping=lambda *a:mapping
    receipts=[]
    with pytest.raises(Held):g.deliver(S,{},'zoho',{},receipts.append,lambda:None)
    assert not receipts
    mapping.zoho_id='new'
    g.deliver_zoho=lambda *a:{'state':'verified','id':'file'}
    assert g.deliver(S,{},'zoho',{'state':'planned','record':''},receipts.append,lambda:None)['state']=='verified'
    assert receipts==[{'state':'planned','record':'new'}]


def test_expired_budget_before_creation_does_not_mark_writing(monkeypatch):
    g=Gateway.__new__(Gateway);g.folder='folder';g.google=lambda:{}
    monkeypatch.setattr(http,'request',lambda *a,**k:Response({},404))
    marked=[]
    with http.budget(1),pytest.raises(TimeoutError):
        g.archive({'id':'asset','name':'f','drive_id':'reserved'},b'file',lambda:None,allow_create=True,before_create=lambda:marked.append(True))
    assert not marked


def test_received_copy_of_shared_mail_is_normal_skip():
    g=Gateway.__new__(Gateway)
    g.mail=lambda *a,**kw:{'labelIds':['INBOX'],'payload':{'parts':[]}}
    assert g.mail_sources({'ownerEmail':'example.invalid','gmailMessageId':'m'})==[]
