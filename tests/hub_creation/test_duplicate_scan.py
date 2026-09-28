"""2,000件超の照合・中断再開・候補見送りを外部I/Oなしで検証する。"""
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import re
from types import SimpleNamespace
import pytest

from src.hub_creation.domain import CreationHeld
from src.hub_creation.duplicate_scan import ZohoDuplicateScan, title_matcher, contact_matcher
from src.infrastructure.http_budget import HttpBudgetExceeded
from src.record_merge.creation_candidates import candidate_context


class Journal:
    def __init__(self): self.row = None; self.saved = []
    def get(self, key): return deepcopy(self.row)
    def save(self, context, fingerprint, checkpoint, *, state='pending', reason=''):
        self.row = {**context, 'inputHash': fingerprint, 'checkpoint': deepcopy(checkpoint), 'state': state, 'reason': reason}
        self.saved.append(deepcopy(self.row))


class Client:
    _api_base_url = 'https://example.test/crm/v2'
    def __init__(self, count=2001):
        self.count = count; self.queries = []; self.interrupt = None; self.duplicate = None; self.delta = []
    def request(self, method, url, *, json_body, idempotent):
        assert method == 'POST' and url.endswith('/crm/v8/coql') and idempotent is True
        query = json_body['select_query']; self.queries.append(query)
        if self.interrupt == len(self.queries): raise HttpBudgetExceeded()
        last = int(re.search(r'id > (\d+)', query)[1])
        if 'Modified_Time' in query:
            rows = [row for row in self.delta if int(row['id']) > last]
        else:
            rows = [{'id': str(i), 'Name': '対象' if i == self.duplicate else '別名'}
                    for i in range(last + 1, min(last + 2000, self.count) + 1)]
        more = bool(rows and int(rows[-1]['id']) < self.count) if 'Modified_Time' not in query else False
        body = {'data': rows, 'info': {'more_records': more}}
        return SimpleNamespace(status_code=200, ok=True, json=lambda: body)
    def get_record(self, module, identifier):
        return {'id':identifier,'Name':'対象'}


def run(scan, name='対象'):
    with candidate_context('chain', 'page', 'notion:page', {'グループ名': name}):
        scan.check('chain', 'CustomModule3', ['Name'], title_matcher('chain', 'Name', name))


@pytest.mark.parametrize('count', [2001, 100001])
def test_large_scan_completes_without_truncation(count):
    journal, client = Journal(), Client(count)
    run(ZohoDuplicateScan(client, journal))
    full = [q for q in client.queries if 'Modified_Time' not in q]
    assert len(full) == (count + 1999) // 2000
    assert journal.row['checkpoint']['phase'] == 'verified'
    assert 'Modified_Time' in client.queries[-1]


def test_interruption_resumes_after_last_complete_page():
    journal, client = Journal(), Client(4001); client.interrupt = 2
    scan = ZohoDuplicateScan(client, journal)
    with pytest.raises(CreationHeld, match='分割処理中'): run(scan)
    assert journal.row['checkpoint']['last_id'] == '2000'
    assert journal.row['state'] == 'pending'
    client.interrupt = None
    run(scan)
    assert 'id > 2000' in client.queries[2]
    assert journal.row['checkpoint']['phase'] == 'verified'


def test_last_page_duplicate_holds_without_advancing_page(monkeypatch):
    monkeypatch.setenv('RECORD_MERGE_ENABLED', 'false')
    journal, client = Journal(), Client(2001); client.duplicate = 2001
    with pytest.raises(CreationHeld, match='一致'): run(ZohoDuplicateScan(client, journal))
    assert journal.row['checkpoint']['last_id'] == '2000'
    assert journal.row['state'] == 'held'


def test_changed_input_restarts_full_scan():
    journal, client = Journal(), Client(4001); client.interrupt = 2
    scan = ZohoDuplicateScan(client, journal)
    with pytest.raises(CreationHeld): run(scan)
    client.interrupt = None
    run(scan, '変更した名前')
    assert 'id > 0' in client.queries[2] and 'Modified_Time' not in client.queries[2]


def test_modified_previously_scanned_record_is_detected(monkeypatch):
    monkeypatch.setenv('RECORD_MERGE_ENABLED', 'false')
    journal, client = Journal(), Client(2001)
    client.delta = [{'id':'1','Name':'対象'}]
    with pytest.raises(CreationHeld, match='一致'): run(ZohoDuplicateScan(client, journal))
    assert journal.row['checkpoint']['phase'] == 'delta'
    assert journal.row['checkpoint']['last_id'] == '0'


def test_verification_from_prior_run_requires_fresh_delta():
    journal, client = Journal(), Client(2001)
    scan = ZohoDuplicateScan(client, journal); run(scan)
    calls = len(client.queries); run(scan)
    assert 'Modified_Time' in client.queries[calls]
    assert len(client.queries) == calls + 1


def test_slow_delta_requires_another_catchup_window(monkeypatch):
    import src.hub_creation.duplicate_scan as module
    initial = datetime(2026,9,28,tzinfo=timezone.utc)
    times = iter([initial, initial, initial+timedelta(seconds=10), initial+timedelta(seconds=11)])
    monkeypatch.setattr(module,'utc_second',lambda:next(times))
    journal, client = Journal(), Client(1)
    run(ZohoDuplicateScan(client,journal))
    assert sum('Modified_Time' in q for q in client.queries) == 2


@pytest.mark.parametrize('body', [
    {}, {'data': [], 'info': {'more_records': True}},
    {'data':[{'id':'1','Name':'a'},{'id':'1','Name':'b'}],'info':{'more_records':False}},
    {'data':[{'id':'2','Name':'a'},{'id':'1','Name':'b'}],'info':{'more_records':False}},
    {'data':[{'id':'1'}],'info':{'more_records':False}},
])
def test_malformed_pages_never_advance(body):
    journal, client = Journal(), Client()
    client.request = lambda *args,**kwargs: SimpleNamespace(status_code=200,ok=True,json=lambda:body)
    with pytest.raises(CreationHeld): run(ZohoDuplicateScan(client,journal))
    assert journal.row['checkpoint']['last_id'] == '0'
    assert journal.row['state'] == 'held'


def test_missing_coql_scope_is_held_without_sensitive_response():
    journal, client = Journal(), Client()
    client.request = lambda *a,**k: SimpleNamespace(status_code=401,ok=False,json=lambda:{'message':'sensitive','code':'OAUTH_SCOPE_MISMATCH'})
    with pytest.raises(CreationHeld, match='COQL読取権限') as caught: run(ZohoDuplicateScan(client,journal))
    assert 'sensitive' not in str(caught.value)
    assert journal.row['state'] == 'held'


def test_dismissed_candidate_is_rechecked_when_page_resumes(monkeypatch):
    import src.hub_creation.duplicate_scan as module
    journal, client = Journal(), Client(2001); client.duplicate = 2001
    observations = []
    def hold(*args,**kwargs):
        observations.append(kwargs['fetch']())
        if len(observations) == 1: raise CreationHeld('候補の確認待ち')
    monkeypatch.setattr(module,'hold_duplicate',hold)
    scan = ZohoDuplicateScan(client,journal)
    with pytest.raises(CreationHeld): run(scan)
    run(scan)
    assert len(observations) == 2
    assert journal.row['checkpoint']['phase'] == 'verified'


def test_comparison_preserves_full_width_and_contact_email_rules():
    assert title_matcher('chain','Name','ABC')({'Name':' ＡＢＣ '})
    match = contact_matcher({'姓':'山田','名':'太郎','メールアドレス':'A@EXAMPLE.TEST'})
    assert match({'Last_Name':'別人','First_Name':'','Email':'a@example.test'})
    with pytest.raises(CreationHeld): match({'Last_Name':[]})


def test_slow_delta_eventually_holds_and_manual_notification_can_resume(monkeypatch):
    import src.hub_creation.duplicate_scan as module
    initial=datetime(2026,9,28,tzinfo=timezone.utc)
    times=iter([initial,initial,initial+timedelta(seconds=6),initial+timedelta(seconds=12),initial+timedelta(seconds=18)])
    monkeypatch.setattr(module,'utc_second',lambda:next(times))
    journal,client=Journal(),Client(1); scan=ZohoDuplicateScan(client,journal)
    with pytest.raises(CreationHeld,match='3巡') as caught: run(scan)
    assert journal.row['state']=='held'
    assert journal.row['checkpoint']['catchup_rounds']==3
    assert '登録元を再通知' in str(caught.value)
    assert sum('Modified_Time' in query for query in client.queries)==3
    monkeypatch.setattr(module,'utc_second',lambda:initial+timedelta(seconds=30))
    run(scan)
    assert journal.row['checkpoint']['phase']=='verified'


def test_budget_shortened_http_timeout_keeps_scan_pending(monkeypatch):
    import requests
    from src.infrastructure.http_budget import http_budget
    from src.sync_engine.clients._http import request_with_retry
    clock=[0.0]
    monkeypatch.setattr('src.infrastructure.http_budget.time.monotonic',lambda:clock[0])
    def request(*args,**kwargs):
        clock[0]+=kwargs['timeout']
        raise requests.exceptions.Timeout()
    monkeypatch.setattr('src.sync_engine.clients._http.requests.request',request)
    def sleep(seconds): clock[0]+=seconds
    journal,client=Journal(),Client()
    client.request=lambda *a,**k:request_with_retry('GET','https://example.test',sleep=sleep)
    with http_budget(30):
        with pytest.raises(CreationHeld,match='分割処理中'): run(ZohoDuplicateScan(client,journal))
    assert journal.row['state']=='pending'
    assert journal.row['checkpoint']['last_id']=='0'


@pytest.mark.parametrize('status', [429, 503])
def test_transient_failures_retry_three_times_then_hold(status):
    journal, client = Journal(), Client()
    client.request = lambda *a, **k: SimpleNamespace(status_code=status, ok=False, json=lambda: {'message': 'private'})
    scan = ZohoDuplicateScan(client, journal)
    for index in range(3):
        with pytest.raises(CreationHeld): run(scan)
        assert journal.row['state'] == ('held' if index == 2 else 'pending')
        assert journal.row['checkpoint']['last_id'] == '0'
        assert 'private' not in journal.row['reason']


def test_connection_failure_recovers_without_losing_checkpoint():
    import requests
    journal, client = Journal(), Client(4001)
    original = client.request
    def request(*args, **kwargs):
        if len(client.queries) == 1: raise requests.exceptions.ConnectionError('private')
        return original(*args, **kwargs)
    client.request = request; scan = ZohoDuplicateScan(client, journal)
    with pytest.raises(CreationHeld, match='分割処理中'): run(scan)
    assert journal.row['checkpoint']['last_id'] == '2000'
    client.request = original
    run(scan)
    assert journal.row['checkpoint']['phase'] == 'verified'


def test_delta_queries_overlap_two_minutes(monkeypatch):
    initial = datetime(2026, 9, 28, tzinfo=timezone.utc)
    monkeypatch.setattr('src.hub_creation.duplicate_scan.utc_second', lambda: initial)
    journal, client = Journal(), Client(1); scan = ZohoDuplicateScan(client, journal)
    run(scan); run(scan)
    for query in (q for q in client.queries if 'Modified_Time' in q):
        assert "Modified_Time >= '2026-09-27T23:58:00+00:00'" in query
        assert "Modified_Time <= '2026-09-28T00:00:00+00:00'" in query


def test_defer_preserves_held_checkpoint_for_explicit_resume():
    journal, client = Journal(), Client()
    scan = ZohoDuplicateScan(client, journal)
    with candidate_context('chain', 'page', 'notion:page', {'グループ名': '対象'}):
        scan.defer()
        journal.row['state'] = 'held'
        journal.row['checkpoint']['catchup_rounds'] = 3
        before = deepcopy(journal.row)
        scan.defer()
        assert journal.row == before


@pytest.mark.parametrize('failure', ['rate_limit', 'timeout'])
def test_real_http_budget_failure_holds_after_six_runs(monkeypatch, failure):
    import requests
    from src.sync_engine.clients._http import request_with_retry
    clock = [0.0]
    monkeypatch.setattr('src.infrastructure.http_budget.time.monotonic', lambda: clock[0])
    def request(*args, **kwargs):
        if failure == 'timeout':
            clock[0] += kwargs['timeout']
            raise requests.exceptions.Timeout()
        return SimpleNamespace(status_code=429, headers={'Retry-After': '30'})
    def sleep(seconds): clock[0] += seconds
    monkeypatch.setattr('src.sync_engine.clients._http.requests.request', request)
    journal, client = Journal(), Client()
    client.request = lambda *a, **k: request_with_retry('GET', 'https://example.test', timeout=30, sleep=sleep)
    scan = ZohoDuplicateScan(client, journal)
    for index in range(6):
        with pytest.raises(CreationHeld): run(scan)
        assert journal.row['state'] == ('held' if index == 5 else 'pending')
        assert journal.row['checkpoint']['no_progress_runs'] == index + 1
        assert journal.row['checkpoint']['last_id'] == '0'
    client.request = Client(1).request
    run(scan)
    assert journal.row['checkpoint']['phase'] == 'verified'
