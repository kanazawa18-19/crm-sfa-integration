"""既存値保護、保存後の中断、通信障害、認証を検証する。"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.contact_name_job.domain import decision, validate_plan
from src.contact_name_job import worker, auth


def entry():
    return {'id': str(uuid4()), 'before': {'名前':'試験 太郎','姓':'','名':''},
            'changes': {'姓':'試験','名':'太郎'}}


def test_decisions_and_existing_value_protection():
    e = entry()
    assert decision(e, e['before']) == 'update'
    assert decision(e, {**e['before'],**e['changes']}) == 'verified'
    assert decision(e, {**e['before'],'姓':'別姓'}) == 'held'
    assert decision(e, {**e['before'],'名前':'別人'}) == 'held'


def test_plan_rejects_unapproved_changes():
    e = entry()
    assert len(validate_plan([e])) == 64
    with pytest.raises(ValueError): validate_plan([e,e])
    e['changes']['メールアドレス']='test@example.invalid'
    with pytest.raises(ValueError): validate_plan([e])


def test_original_blank_null_plan_is_supported():
    e=entry();e['before']['姓']=None;e['before']['名']=None
    assert len(validate_plan([e]))==64
    assert decision(e, e['before'])=='update'


def test_name_client_does_not_expand_relations(monkeypatch):
    client=worker.NameOnlyClient('contact','test',api_key='synthetic')
    monkeypatch.setattr(client,'get_raw_page',lambda key:{'id':key})
    assert client.get_raw_page_with_relations('page',{'取引先マスター'})=={'id':'page'}


def test_write_succeeded_but_read_failed_can_resume_without_second_write(monkeypatch):
    e=entry()
    monkeypatch.setattr(worker,'record_lock',lambda *a:nullcontext(lambda:None))
    class Gateway:
        current=deepcopy(e['before'])
        writes=0
        fail=False
        def live(self,*a):
            if self.fail:
                self.fail=False
                raise ConnectionError()
            return self.current.copy()
        def client(self,*a):return self
        def update_page(self,key,changes):
            self.current.update(changes);self.writes+=1;self.fail=True
    g=Gateway()
    with pytest.raises(ConnectionError):worker.complete_one(e,g)
    assert worker.complete_one(e,g)=='verified'
    assert g.writes==1


def test_full_readback_mismatch_is_not_success(monkeypatch):
    e=entry();monkeypatch.setattr(worker,'record_lock',lambda *a:nullcontext(lambda:None))
    class Gateway:
        current=e['before']
        def live(self,*a):return self.current
        def client(self,*a):return self
        def update_page(self,*a):self.current={**e['before'],**e['changes'],'名前':'変更された名前'}
    with pytest.raises(ValueError):worker.complete_one(e,Gateway())


def test_lost_shard_lock_prevents_write(monkeypatch):
    e=entry();monkeypatch.setattr(worker,'record_lock',lambda *a:nullcontext(lambda:None))
    class Gateway:
        def live(self,*a):return e['before']
        def client(self,*a):raise AssertionError('ロックを失った実行は書き込まない')
    def lost():raise ConnectionError('切断済み')
    with pytest.raises(ConnectionError):worker.complete_one(e,Gateway(),lost)


class Repo:
    def __init__(self):self.finished=[];self.enabled=True;self.locked=True
    @contextmanager
    def shard(self,n):yield object() if self.locked else None
    def active(self,c):return self.enabled
    def started(self,c):pass
    def next(self,c,n):return {'plan':entry()}
    def finish(self,c,e,state,error_type=None,**kwargs):self.finished.append((state,error_type))


def test_connection_failure_stops_batch_and_records_only_exception_class(monkeypatch):
    repo=Repo()
    def fail(*a):raise ConnectionError('秘密情報を含み得る本文')
    monkeypatch.setattr(worker,'complete_one',fail)
    result=worker.run_shard(0,repository=repo,gateway=object())
    assert result['retry']==1
    assert repo.finished==[('retry','ConnectionError')]


@pytest.mark.parametrize('enabled,locked,status',[(False,True,'paused'),(True,False,'busy')])
def test_paused_and_duplicate_do_not_write(enabled,locked,status):
    repo=Repo();repo.enabled=enabled;repo.locked=locked
    assert worker.run_shard(0,repository=repo)['status']==status
    assert repo.finished==[]


def test_identity_checks_audience_and_subject(monkeypatch):
    monkeypatch.setenv('CONTACT_NAME_GAS_AUDIENCE','dedicated-script')
    monkeypatch.setenv('CONTACT_NAME_GAS_SUBJECT','owner')
    def verify(token, request, audience, **kwargs):
        assert audience=='dedicated-script'
        return {'sub':token}
    monkeypatch.setattr(auth,'verify_oauth2_token',verify)
    auth.verify_gas_identity('Bearer owner')
    with pytest.raises(HTTPException):auth.verify_gas_identity('Bearer other')
    monkeypatch.delenv('CONTACT_NAME_GAS_AUDIENCE')
    with pytest.raises(HTTPException):auth.verify_gas_identity('Bearer owner')


def test_gateway_requires_separate_connection(monkeypatch):
    monkeypatch.delenv('NOTION_API_KEY_2',raising=False)
    monkeypatch.setenv('NOTION_API_KEY','通常同期用を流用しない')
    with pytest.raises(KeyError):worker.JobGateway(0)


def test_real_signed_identity_transport_and_claim_validation(monkeypatch):
    import json,time
    from types import SimpleNamespace
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.auth import crypt,jwt
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    private=key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())
    public=key.public_key().public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo)
    signer=crypt.RSASigner.from_string(private,key_id='synthetic')
    def transport(url,method,timeout):
        assert url.startswith('https://www.googleapis.com/') and timeout==10
        return SimpleNamespace(status=200,data=json.dumps({'synthetic':public.decode()}).encode())
    monkeypatch.setattr(auth,'Request',lambda:transport)
    monkeypatch.setenv('CONTACT_NAME_GAS_AUDIENCE','dedicated')
    monkeypatch.setenv('CONTACT_NAME_GAS_SUBJECT','owner')
    payload={'iss':'https://accounts.google.com','sub':'owner','aud':'dedicated','iat':int(time.time()),'exp':int(time.time())+300}
    token=jwt.encode(signer,payload).decode()
    auth.verify_gas_identity('Bearer '+token)
    for changes in ({'aud':'other'},{'sub':'other'},{'exp':int(time.time())-100},{'iss':'https://invalid.example'}):
        token=jwt.encode(signer,{**payload,**changes}).decode()
        with pytest.raises(HTTPException):auth.verify_gas_identity('Bearer '+token)
