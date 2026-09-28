"""認証を含むHTTP再試行が親の時間予算を越えて待たない。"""
from types import SimpleNamespace
import pytest
from src.infrastructure.http_budget import http_budget, remaining, HttpBudgetExceeded
from src.sync_engine.clients._http import request_with_retry


def test_nested_budget_cannot_extend_parent(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('src.infrastructure.http_budget.time.monotonic',lambda:clock[0])
    with http_budget(3):
        with http_budget(100):
            clock[0] = 3
            with pytest.raises(HttpBudgetExceeded): remaining()
    assert remaining() is None


def test_rate_limit_wait_stops_before_deadline(monkeypatch):
    calls=[]; sleeps=[]
    def request(*a,**kw):
        calls.append(kw)
        return SimpleNamespace(status_code=429,headers={'Retry-After':'30'})
    monkeypatch.setattr('src.sync_engine.clients._http.requests.request',request)
    with http_budget(1):
        with pytest.raises(HttpBudgetExceeded): request_with_retry('GET','https://example.test',sleep=sleeps.append)
    assert len(calls)==1 and not sleeps
    assert calls[0]['timeout'] <= .5


def test_unknown_post_is_not_retried_with_budget(monkeypatch):
    import requests
    calls=[]
    def request(*a,**kw): calls.append(kw); raise requests.exceptions.Timeout()
    monkeypatch.setattr('src.sync_engine.clients._http.requests.request',request)
    with http_budget(100):
        with pytest.raises(requests.exceptions.Timeout):
            request_with_retry('POST','https://example.test',idempotent=False)
    assert len(calls)==1


def test_token_refresh_is_inside_same_budget(monkeypatch):
    from src.sync_engine.clients.zoho_client import HttpZohoClient
    client=HttpZohoClient(client_id='synthetic',client_secret='synthetic',refresh_token='synthetic')
    calls=[]
    def request(*a,**kw):
        calls.append(kw)
        return SimpleNamespace(status_code=429,headers={'Retry-After':'30'})
    monkeypatch.setattr('src.sync_engine.clients._http.requests.request',request)
    with http_budget(1):
        with pytest.raises(HttpBudgetExceeded): client.get_record('Accounts','1')
    assert len(calls)==1
    assert not client._token_lock.locked()


def test_shortened_timeout_is_budget_exhaustion_not_permanent_failure(monkeypatch):
    import requests
    clock=[0.0]; calls=[]
    monkeypatch.setattr('src.infrastructure.http_budget.time.monotonic',lambda:clock[0])
    def request(*args,**kwargs):
        calls.append(kwargs['timeout'])
        clock[0]+=kwargs['timeout']
        raise requests.exceptions.Timeout()
    monkeypatch.setattr('src.sync_engine.clients._http.requests.request',request)
    def sleep(seconds): clock[0]+=seconds
    with http_budget(30):
        with pytest.raises(HttpBudgetExceeded):
            request_with_retry('GET','https://example.test',timeout=10,max_retries=3,sleep=sleep)
    assert calls==[10,9.75]
    assert clock[0]<30


def test_shortened_unknown_post_still_runs_only_once(monkeypatch):
    import requests
    calls=[]
    def request(*args,**kwargs):
        calls.append(kwargs)
        raise requests.exceptions.Timeout()
    monkeypatch.setattr('src.sync_engine.clients._http.requests.request',request)
    with http_budget(1):
        with pytest.raises(HttpBudgetExceeded):
            request_with_retry('POST','https://example.test',idempotent=False)
    assert len(calls)==1
