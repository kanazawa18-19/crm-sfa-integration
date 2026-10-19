"""保存成功後の切断・二重実行・境界ページを検証する。"""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from src.contact_affiliation_job import worker
from src.contact_affiliation_job.gateway import JobGateway


class Conn:
    def execute(self, *args):
        pass


class Repo:
    def __init__(self):
        self.item = {'notionId': str(uuid4()), 'state': 'pending', 'decision': {}}
        self.history = []
    def save(self, conn, key, state, decision, error=None):
        self.item.update(state=state, decision=deepcopy(decision))
        self.history.append(state)


class Gateway:
    before_write = None
    def __init__(self, key):
        self.key = key
        self.current = {'メールアドレス': 'test@example.invalid', '取引先マスター': [], '_last_edited_time': 'unchanged'}
        self.writes = 0
        self.delivery = 'delivered'
        self.fail_after_write = False
    def live(self, kind, key):
        if kind == 'client_master':
            return {'取引先名': '試験株式会社'}
        if self.fail_after_write and self.writes:
            self.fail_after_write = False
            raise ConnectionError()
        return deepcopy(self.current)
    def contacts(self, addresses):
        return [{'id': self.key}]
    def collect(self, *args):
        stamp = datetime.now(timezone.utc).isoformat()
        return [{'email': 'test@example.invalid', 'origin': source, 'source_id': source,
                 'observed_at': stamp, 'company_name': '試験株式会社', 'explicit': True} for source in ('a','b')], [{'id': 'company', 'name': '試験株式会社'}]
    def client(self, kind):
        return self
    def update_page(self, key, patch):
        if self.before_write:
            self.before_write()
        self.writes += 1
        self.current.update(patch)
    def dispatch(self, *args, **kwargs):
        return self.delivery


@pytest.fixture
def fixture(monkeypatch):
    monkeypatch.setattr(worker, 'record_lock', lambda *args: nullcontext(lambda: None))
    repo = Repo()
    return repo, Gateway(repo.item['notionId']), Conn()


def test_dry_run_writes_no_notion(fixture):
    repo, gateway, conn = fixture
    assert worker.process(repo.item.copy(), repo, conn, gateway, True) == 'candidate'
    assert gateway.writes == 0


def test_normal_save_and_actual_delivery(fixture):
    repo, gateway, conn = fixture
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'verified'
    assert gateway.writes == 1
    assert repo.history == ['writing', 'delivery_pending', 'verified']


def test_unknown_write_resumes_without_duplicate(fixture):
    repo, gateway, conn = fixture
    gateway.fail_after_write = True
    with pytest.raises(ConnectionError):
        worker.process(repo.item.copy(), repo, conn, gateway, False)
    assert repo.item['state'] == 'writing'
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'verified'
    assert gateway.writes == 1


def test_partial_delivery_stays_pending(fixture):
    repo, gateway, conn = fixture
    gateway.delivery = 'partial'
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'delivery_pending'
    gateway.delivery = 'delivered'
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'verified'
    assert gateway.writes == 1


def test_existing_affiliation_never_overwritten(fixture):
    repo, gateway, conn = fixture
    gateway.current['取引先マスター'] = ['other']
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'existing'
    assert gateway.writes == 0


def test_record_lock_loss_prevents_write(fixture, monkeypatch):
    repo, gateway, conn = fixture
    def lost():
        raise ConnectionError()
    monkeypatch.setattr(worker, 'record_lock', lambda *args: nullcontext(lost))
    with pytest.raises(ConnectionError):
        worker.process(repo.item.copy(), repo, conn, gateway, False)
    assert gateway.writes == 0


def test_discovery_crosses_10000_limit(monkeypatch):
    gateway = JobGateway()
    class Client:
        def query_raw(self, body):
            assert body['filter']['created_time']['on_or_after'] == '2020-01-01'
            return {'results': [{'id': 'last', 'created_time': '2021-01-01'}], 'has_more': False,
                    'request_status': {'type': 'incomplete'}}
    monkeypatch.setattr(gateway, 'client', lambda kind: Client())
    pages, cursor = gateway.discover({'watermark': '2020-01-01', 'count': 9999})
    assert cursor == {'watermark': '2021-01-01', 'count': 0}


def test_discovery_stalled_timestamp_is_error(monkeypatch):
    gateway = JobGateway()
    class Client:
        def query_raw(self, body):
            return {'results': [{'id': 'last', 'created_time': '2020-01-01'}], 'has_more': False}
    monkeypatch.setattr(gateway, 'client', lambda kind: Client())
    with pytest.raises(ValueError):
        gateway.discover({'watermark': '2020-01-01', 'count': 8999})


def test_duplicate_email_is_known_hold_not_network_failure(fixture):
    repo, gateway, conn = fixture
    gateway.contacts = lambda addresses: [{'id': gateway.key}, {'id': 'other'}]
    with pytest.raises(worker.DecisionHold, match='一意'):
        worker.process(repo.item.copy(), repo, conn, gateway, False)
    assert gateway.writes == 0


def test_dry_run_rejects_duplicate_email(fixture):
    repo, gateway, conn = fixture
    gateway.contacts = lambda addresses: [{'id': gateway.key}, {'id': 'other'}]
    with pytest.raises(worker.DecisionHold, match='一意'):
        worker.process(repo.item.copy(), repo, conn, gateway, True)
    assert gateway.writes == 0


def test_dry_run_rechecks_company_name(fixture):
    repo, gateway, conn = fixture
    live = gateway.live
    gateway.live = lambda kind, key: {'取引先名': '別の株式会社'} if kind == 'client_master' else live(kind, key)
    with pytest.raises(worker.DecisionHold, match='取引先名'):
        worker.process(repo.item.copy(), repo, conn, gateway, True)
    assert gateway.writes == 0


def test_post_write_email_normalizes_spaces_and_case(fixture):
    repo, gateway, conn = fixture
    gateway.current['メールアドレス'] = ' TEST@EXAMPLE.INVALID '
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'verified'


def test_delivery_pending_manual_removal_never_repatches(fixture):
    repo, gateway, conn = fixture
    gateway.delivery = 'partial'
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'delivery_pending'
    gateway.current['取引先マスター'] = []
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'released'
    assert gateway.writes == 1
    assert repo.item['state'] == 'released'


def test_delivery_pending_removal_between_reads_never_repatches(fixture):
    repo, gateway, conn = fixture
    gateway.delivery = 'partial'
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'delivery_pending'
    live = gateway.live
    reads = 0
    def changing(kind, key):
        nonlocal reads
        if kind == 'contact':
            reads += 1
            if reads == 2:
                gateway.current['取引先マスター'] = []
        return live(kind, key)
    gateway.live = changing
    assert worker.process(repo.item.copy(), repo, conn, gateway, False) == 'released'
    assert gateway.writes == 1
