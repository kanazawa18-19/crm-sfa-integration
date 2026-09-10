"""独立QAの期限・再試行回帰試験。SDK通信と台帳操作はローカルの代替に固定する。"""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from src.sync_capacity.deadline import Deadline, using_deadline, CapacityDeadlineExceeded, current_deadline
from src.sync_capacity.firestore_store import FirestoreJobStore, Settings
from src.sync_capacity.application import drain_one
from src.sync_capacity.domain import Claim

@pytest.fixture
def clock(monkeypatch):
    value = [0.0]
    monkeypatch.setattr('src.sync_capacity.deadline.time.monotonic', lambda: value[0])
    monkeypatch.setattr('src.sync_capacity.deadline.time.sleep', lambda s: value.__setitem__(0, value[0] + s))
    monkeypatch.setattr('src.sync_capacity.firestore_store.random.uniform', lambda *a: 0.0)
    return value

def test_query_candidates_share_deadline(clock):
    store = FirestoreJobStore(Settings('demo-qa', '(default)', 'qa', 1), client=Mock())
    refs = [Mock(), Mock()]
    candidates = [SimpleNamespace(reference=r) for r in refs]
    query = store.jobs.where.return_value.order_by.return_value.limit.return_value

    def stream(**kw):
        assert kw['timeout'] == 1.0
        clock[0] = 0.4
        return candidates
    query.stream.side_effect = stream
    scopes = []

    def scope(tx):
        scopes.append(current_deadline.get().expires_at)
        clock[0] += 0.4
        return {'slots': {'0': None}}
    store._scope = scope
    refs[0].get.return_value = SimpleNamespace(exists=False)
    with pytest.raises(CapacityDeadlineExceeded):
        store.claim('owner', 10, deadline=Deadline.after(1))
    assert scopes == [1.0]
    refs[0].get.assert_called_once()
    refs[1].get.assert_not_called()

def test_finish_budget_insufficient_keeps_claim(clock):
    store = Mock()
    store.claim.return_value = Claim('job', 'owner', '0', 'notion', {})

    def execute():
        clock[0] = 29
        return ({'statusCode': 200}, False)
    with pytest.raises(CapacityDeadlineExceeded):
        drain_one(store, lambda c: execute, deadline=Deadline.after(30), execution_budget=10, finish_budget=5)
    store.claim.assert_called_once()
    store.finish.assert_not_called()

@pytest.mark.parametrize('timeout', [0.2, 2.0])
def test_guard_short_timeout_retained(clock, timeout):
    from scripts.capacity_trial.firestore import GuardedAPI
    from scripts.capacity_trial.guard import BASE
    raw = Mock()
    raw.batch_get_documents.return_value = []
    ledger = Mock()
    ledger.upper_bound.return_value = None
    api = GuardedAPI(raw, ledger, role='runner')
    with using_deadline(Deadline.after(20)):
        list(api.batch_get_documents(request={'database': BASE, 'documents': [BASE + '/documents/sync_capacity_scopes/trial-smoke']}, timeout=timeout))
    assert raw.batch_get_documents.call_args.kwargs['timeout'] == timeout
    assert raw.batch_get_documents.call_args.kwargs['retry'] is None

def test_guard_reservation_delay_blocks_rpc_without_refund(clock):
    from scripts.capacity_trial.firestore import GuardedAPI
    from scripts.capacity_trial.guard import BASE
    raw = Mock()
    ledger = Mock()
    ledger.upper_bound.return_value = None
    ledger.reserve.side_effect = lambda **kw: clock.__setitem__(0, 2)
    api = GuardedAPI(raw, ledger, role='runner')
    with using_deadline(Deadline.after(1)), pytest.raises(CapacityDeadlineExceeded):
        api.batch_get_documents(request={'database': BASE, 'documents': [BASE + '/documents/sync_capacity_scopes/trial-smoke']})
    ledger.reserve.assert_called_once()
    raw.batch_get_documents.assert_not_called()

def test_ledger_lock_wait_expires_without_mutation(clock, tmp_path, monkeypatch):
    from scripts.capacity_trial.guard import Ledger
    path = tmp_path / 'ledger.json'
    path.write_text('{"unchanged":true}')
    locked = Mock(side_effect=BlockingIOError())
    monkeypatch.setattr('scripts.capacity_trial.guard.fcntl.flock', locked)
    operation = Mock()
    with using_deadline(Deadline.after(0.08)), pytest.raises(CapacityDeadlineExceeded):
        Ledger(path).transact(operation)
    assert locked.call_count >= 2
    operation.assert_not_called()
    assert path.read_text() == '{"unchanged":true}'

def test_guard_additional_read_budget_is_shared(clock, monkeypatch):
    from scripts.capacity_trial.firestore import GuardedAPI
    from scripts.capacity_trial.guard import BASE
    from google.cloud.firestore_v1.types import Write, Document
    from google.cloud.firestore_v1 import _helpers
    raw = Mock()
    raw.batch_get_documents.return_value = []
    ledger = Mock()
    ledger.upper_bound.return_value = {'basis': 'reserved-upper-bound'}
    api = GuardedAPI(raw, ledger, role='runner')

    def full_document(change, read_existing):
        read_existing(change.update.name)
        clock[0] = 1.0
        read_existing(change.update.name)
    monkeypatch.setattr('scripts.capacity_trial.small_policy.full_document', full_document)
    write = Write(update=Document(name=BASE + '/documents/sync_capacity_scopes/trial-smoke/jobs/qa', fields=_helpers.encode_dict({'state': 'completed'})))
    with using_deadline(Deadline.after(1)), pytest.raises(CapacityDeadlineExceeded):
        api.commit(request={'database': BASE, 'writes': [write]})
    raw.batch_get_documents.assert_called_once()
    raw.commit.assert_not_called()
    ledger.reserve.assert_called_once()

@pytest.mark.parametrize('conflicts', [1, 2, 8])
def test_real_batches_reread_and_attempt_limit(clock, monkeypatch, conflicts):
    from datetime import datetime, timezone
    from google.cloud import firestore
    from google.auth.credentials import AnonymousCredentials
    from google.api_core.exceptions import FailedPrecondition
    from google.cloud.firestore_v1.services.firestore import FirestoreClient
    from google.cloud.firestore_v1.types import CommitResponse
    client = firestore.Client(project='demo-independent', credentials=AnonymousCredentials())
    store = FirestoreJobStore(Settings('demo-independent', '(default)', 'qa', 1), client=client)
    stamp = datetime.now(timezone.utc)
    snapshot = firestore.DocumentSnapshot(store.scope_ref, {'version': 1, 'limit': 1, 'slots': {'0': None}}, True, stamp, stamp, stamp)
    read = Mock(return_value=snapshot)
    monkeypatch.setattr(store.scope_ref, 'get', read)
    calls = []

    def commit(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) <= conflicts:
            clock[0] += 3.181 if len(calls) == 1 else 0.01
            raise FailedPrecondition('synthetic')
        return CommitResponse()
    monkeypatch.setattr(FirestoreClient, 'commit', commit)
    batches = []

    def op(batch):
        batches.append(batch)
        scope = store._scope(batch)
        batch.update(store.scope_ref, {'version': scope['version']})
        return 'ok'
    if conflicts == 8:
        with pytest.raises(FailedPrecondition) as caught:
            store._atomic(op)
        assert caught.value.capacity_atomic['stop_reason'] == 'attempt_limit'
        expected = 8
    else:
        assert store._atomic(op) == 'ok'
        expected = conflicts + 1
    assert len(calls) == read.call_count == len(batches) == expected
    assert len(set(map(id, batches))) == expected

def test_retry_observes_full_scope(clock, monkeypatch):
    from src.sync_capacity.firestore_store import _ComparedBatch
    from google.api_core.exceptions import FailedPrecondition
    store = FirestoreJobStore(Settings('demo-qa', '(default)', 'qa', 1), client=Mock())
    ref = Mock(id='job')
    store.jobs.where.return_value.order_by.return_value.limit.return_value.stream.return_value = [SimpleNamespace(reference=ref)]
    store._scope = Mock(side_effect=[{'slots': {'0': None}}, {'slots': {'0': {'owner': 'other'}}}])
    ref.get.return_value = SimpleNamespace(exists=True, to_dict=lambda: {'state': 'pending', 'available_at': 0, 'attempts': 0, 'source': 'notion', 'event': {}})
    commits = []

    def commit(batch):
        commits.append(len(batch.changes))
        if batch.changes:
            raise FailedPrecondition('synthetic')
    monkeypatch.setattr(_ComparedBatch, 'update', lambda batch, ref, data: batch.changes.update({id(ref): data}))
    monkeypatch.setattr(_ComparedBatch, 'commit', commit)
    assert store.claim('owner', 10) is None
    assert store._scope.call_count == 2
    assert commits == [2, 0]
    ref.get.assert_called_once()


@pytest.mark.parametrize("explicit", [False, True])
def test_short_parent_deadline_stops_before_claim(clock, explicit):
    store = Mock()
    prepare = Mock()
    options = {"deadline": Deadline.after(300)} if explicit else {}
    with using_deadline(Deadline.after(2)), pytest.raises(CapacityDeadlineExceeded):
        drain_one(store, prepare, **options)
    store.claim.assert_not_called()
    prepare.assert_not_called()
    store.finish.assert_not_called()


def test_parent_deadline_expiring_in_prepare_never_executes(clock):
    store = Mock()
    store.claim.return_value = Claim("job", "owner", "0", "notion", {})
    execute = Mock()
    def prepare(_):
        clock[0] = 3.0
        return execute
    with using_deadline(Deadline.after(2)), pytest.raises(CapacityDeadlineExceeded):
        drain_one(store, prepare, deadline=Deadline.after(300),
                  execution_budget=0.5, finish_budget=0.5)
    store.claim.assert_called_once()
    execute.assert_not_called()
    store.finish.assert_not_called()


def test_finish_deadline_records_not_started(clock, monkeypatch):
    emitted = Mock()
    monkeypatch.setattr("src.sync_capacity.application.emit", emitted)
    store = Mock()
    store.claim.return_value = Claim("job", "owner", "0", "notion", {})
    def execute():
        clock[0] = 29
        return {"statusCode": 200}, False
    with pytest.raises(CapacityDeadlineExceeded):
        drain_one(store, lambda _: execute, deadline=Deadline.after(30),
                  execution_budget=10, finish_budget=5)
    store.finish.assert_not_called()
    events = [call.args[0] for call in emitted.call_args_list]
    assert events[-1] == "finish_not_started"
    assert "finish_started" not in events
    assert "finish_unconfirmed" not in events
    assert emitted.call_args.kwargs["finish_seconds"] is None


def test_claim_delay_records_initialization_not_started(clock, monkeypatch):
    emitted = Mock()
    monkeypatch.setattr("src.sync_capacity.application.emit", emitted)
    store = Mock()
    def claim(*args):
        clock[0] = 20
        return Claim("job", "owner", "0", "notion", {})
    store.claim.side_effect = claim
    prepare = Mock()
    assert drain_one(store, prepare, deadline=Deadline.after(30),
                     execution_budget=10, finish_budget=5)["state"] == "retry"
    prepare.assert_not_called()
    event = next(call for call in emitted.call_args_list
                 if call.args[0] == "initialization_not_started")
    assert event.kwargs["initialization_seconds"] is None
    assert store.finish.call_args.args[1:3] == ("retry", "initialization_not_started")
