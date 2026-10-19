"""実通信を使わず、重複・中断・手動解除・原本変更を再現する。"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace
import pytest

from src.attachment_sync.domain import Source, Held, content_key, drive_id, notion_download_url
from src.attachment_sync.worker import process
from src.attachment_sync.gateway import Gateway


S = Source('gmail', 'action', 'page', '添付ファイル', 'message', 'part', '見積.pdf', 4, owner='example.invalid', field='review')


class Repo:
    def __init__(self): self.assets = {}; self.rows = {}; self.result = None
    def check(self): pass
    def asset(self, key): return deepcopy(self.assets.get(key))
    def save_asset(self, key, value): self.assets[key] = deepcopy(value)
    def finish(self, key, state, asset_id=None, reason=None):
        self.result = state
        self.rows.setdefault(key, {}).update(state=state)
        if asset_id: self.rows[key]['assetId'] = asset_id
    def mark_mirror(self, source, key): self.rows[source.key] = {'state': 'mirrored', 'assetId': key}


@contextmanager
def lock(*_): yield lambda: None


class FakeGateway:
    config = {'action': {'kintone_field': 'CRM添付ファイル'}}
    def __init__(self): self.creates=0; self.writes={}; self.data=b'file'; self.remote=False; self.fail=None
    def source_guard(self, source): return nullcontext(lambda:None)
    def validate_source(self, source): return self.data
    def reserve_drive_id(self): return 'reserved'
    def archive(self, asset, data, check, allow_create=False, before_create=lambda:None):
        if not self.remote:
            if not allow_create: raise Held('結果不明')
            before_create()
            self.remote=True; self.creates+=1
        if self.fail=='drive': self.fail=None; raise TimeoutError()
        return 'https://drive.google.com/file/d/reserved/view'
    def deliver(self, source, asset, target, receipt, checkpoint, check):
        if target in self.writes: return {'state': 'verified', 'record':target, 'id':target}
        if receipt.get('state') in ('writing','verified'): raise Held('解除または結果不明')
        checkpoint({'state':'writing','record':target})
        self.writes[target]=True
        if self.fail==target: self.fail=None; raise TimeoutError()
        return {'state':'verified','record':target,'id':target}


def row(repo): return {'data':asdict(S), **repo.rows.get(S.key,{})}


@pytest.mark.parametrize('failure', ['drive','notion','zoho','kintone'])
def test_timeout_resumes_without_second_create(failure):
    repo=Repo(); gateway=FakeGateway(); gateway.fail=failure
    assert process(repo,gateway,row(repo),lock)=='retry'
    assert process(repo,gateway,row(repo),lock)=='verified'
    assert gateway.creates==1 and len(gateway.writes)==3


def test_removed_after_uncertain_write_is_not_restored():
    repo=Repo(); gateway=FakeGateway(); gateway.fail='notion'
    process(repo,gateway,row(repo),lock)
    del gateway.writes['notion']
    assert process(repo,gateway,row(repo),lock)=='held'
    assert not gateway.writes


def test_source_changed_during_retry_is_held():
    repo=Repo(); gateway=FakeGateway(); gateway.fail='drive'
    process(repo,gateway,row(repo),lock)
    gateway.data=b'changed'
    assert process(repo,gateway,row(repo),lock)=='held'
    assert not gateway.writes


def test_identical_bytes_dedupe_within_record_and_category():
    assert content_key(S,b'file')==content_key(replace(S,file_id='other'),b'file')
    assert content_key(S,b'file')!=content_key(replace(S,record='another'),b'file')


@pytest.mark.parametrize('url', ['http://127.0.0.1/x','https://example.invalid/file','https://s3.us-west-2.amazonaws.com/another/file','https://prod-files-secure.s3.us-west-2.amazonaws.com@127.0.0.1/file'])
def test_not_arbitrary_url(url):
    with pytest.raises(Held): notion_download_url(url)


def test_no_inline_mail_images():
    gateway=Gateway.__new__(Gateway)
    raw={'payload':{'parts':[
        {'filename':'logo.png','headers':[{'name':'Content-Disposition','value':'inline'}]},
        {'filename':'quote.pdf','partId':'2','headers':[{'name':'Content-Disposition','value':'attachment'}]}
    ]}}
    assert [p['filename'] for p in gateway.mail_parts(raw)]==['quote.pdf']


def test_zoho_uncertain_name_is_not_success_or_duplicate(monkeypatch):
    gateway=Gateway.__new__(Gateway)
    asset={'id':'a'*64,'name':'quote.pdf','url':'https://drive.google.com/file/d/id/view'}
    gateway.zattachments=lambda *a:[{'File_Name':'添付ファイル：quote.pdf [CRM:'+'a'*16+']','id':'other'}]
    with pytest.raises(Held): gateway.deliver_zoho(S,asset,'record',{'state':'writing'},lambda _:None,lambda:None)


def test_notion_uses_append_and_keeps_existing_properties():
    gateway=Gateway.__new__(Gateway); gateway.page=lambda *a:{}
    written=[]
    class Response:
        status_code=200
        ok=True
        def __init__(self,data):self.data=data
        def json(self):return self.data
    class Client:
        def _request(self,method,path,**kwargs):
            if method=='PATCH':
                written.append(kwargs['json_body']);return Response({})
            return Response({'results': [{'id':'plain','paragraph':{'rich_text':[{'text':{'content':'本文','link':None}}]}}]+([{'id':'block',**written[0]['children'][0]}] if written else [])})
    gateway.notion=lambda db:Client()
    result=gateway.deliver_notion(S,{'name':'quote.pdf','url':'https://drive.google.com/file/d/id/view'},{},lambda _:None,lambda:None)
    assert result['state']=='verified'
    assert 'children' in written[0] and 'properties' not in written[0]


def test_stale_target_mapping_is_held():
    gateway=Gateway.__new__(Gateway)
    gateway.mapping=lambda *a:SimpleNamespace(zoho_id='new',kintone_id='k')
    with pytest.raises(Held):gateway.deliver(S,{},'zoho',{'state':'writing','record':'old'},lambda _:None,lambda:None)


def test_original_target_receipt_never_recreates_deleted_source():
    gateway=Gateway.__new__(Gateway)
    gateway.mapping=lambda *a:SimpleNamespace(zoho_id='z',kintone_id='k')
    receipt={'state':'source','record':'z','id':'original'}
    assert gateway.deliver(S,{},'zoho',receipt,lambda _:None,lambda:None)==receipt
