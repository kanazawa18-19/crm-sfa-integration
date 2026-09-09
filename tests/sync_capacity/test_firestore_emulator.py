"""公式Firestore emulator用。設定が無い通常テストでは実サービスへ接続しない。"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from src.sync_capacity.domain import CapacityUnavailable, OwnerMismatch, PayloadConflict, submission
from src.sync_capacity.firestore_store import FirestoreJobStore, Settings

pytestmark = pytest.mark.skipif(not os.environ.get("FIRESTORE_EMULATOR_HOST"),
                                reason="local Firestore emulator required")


@pytest.fixture
def stores():
    settings = Settings("demo-crm-capacity", "(default)", "test-" + uuid.uuid4().hex, 2)
    a, b = FirestoreJobStore(settings), FirestoreJobStore(settings)
    a.initialize(apply=True)
    return a, b


def put(store, index, now=10):
    item = submission("notion", {"id": f"event-{index}"}, None, now)
    assert store.enqueue(item, now) == "pending"
    return item


def test_shared_slots_saturation_and_later_drain(stores):
    a, b = stores
    for index in range(3):
        put(a, index)
    c1, c2 = a.claim("owner-1", 10), b.claim("owner-2", 10)
    assert c1 and c2 and c1.job_id != c2.job_id
    assert a.claim("owner-3", 10) is None
    a.finish(c1, "completed", "processed", 11)
    c3 = b.claim("owner-3", 12)
    assert c3 and c3.job_id not in {c1.job_id, c2.job_id}


def test_two_workers_do_not_claim_same_job(stores):
    a, b = stores
    put(a, 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda pair: pair[0].claim(pair[1], 10), [(a, "a"), (b, "b")]))
    assert sum(claim is not None for claim in claims) == 1


def test_owner_mismatch_and_no_ttl_reassignment(stores):
    a, b = stores
    put(a, 1)
    claim = a.claim("owner", 10)
    with pytest.raises(OwnerMismatch):
        b.finish(replace(claim, owner="wrong"), "completed", "processed", 11)
    assert b.claim("other", 10**12) is None
    assert a.jobs.document(claim.job_id).get().to_dict()["state"] == "processing"


def test_recovery_requires_owner_and_explicit_stop(stores):
    a, b = stores
    put(a, 1)
    claim = a.claim("owner", 10)
    assert b.recover(claim.job_id, "owner", now=20)["apply"] is False
    assert a.jobs.document(claim.job_id).get().to_dict()["state"] == "processing"
    with pytest.raises(OwnerMismatch):
        b.recover(claim.job_id, "wrong", now=20, apply=True, stopped=True)
    with pytest.raises(ValueError):
        b.recover(claim.job_id, "owner", now=20, apply=True)
    b.recover(claim.job_id, "owner", now=20, apply=True, stopped=True)
    assert a.jobs.document(claim.job_id).get().to_dict()["state"] == "needs_attention"
    assert all(value is None for value in a.scope_ref.get().to_dict()["slots"].values())
    assert b.claim("next", 30) is None


def test_scope_mismatch_never_changes_shared_capacity(stores):
    a, _ = stores
    wrong = FirestoreJobStore(replace(a.settings, slots=3))
    with pytest.raises(CapacityUnavailable):
        wrong.enqueue(submission("notion", {"id": "1"}, None, 10), 10)
    assert a.scope_ref.get().to_dict()["limit"] == 2


def test_duplicate_preserves_first_receipt_and_conflict_is_rejected(stores):
    a, b = stores
    item = put(a, 1)
    assert b.enqueue(item, 20) == "pending"
    changed = submission("notion", {"id": "event-1", "extra": True}, None, 30)
    with pytest.raises(PayloadConflict):
        b.enqueue(changed, 30)
    assert a.jobs.document(item.job_id).get().to_dict()["created_at"] == 10


def test_stale_scope_precondition_prevents_job_creation(stores):
    from google.api_core.exceptions import FailedPrecondition
    from src.sync_capacity.firestore_store import _ComparedBatch
    a, b = stores
    batch = _ComparedBatch(a.client)
    a._scope(batch)
    ref = a.jobs.document("must-not-exist")
    batch.create(ref, {"state": "pending"})
    b.scope_ref.update({"limit": 3})
    with pytest.raises(FailedPrecondition):
        batch.commit()
    assert not ref.get().exists
    assert b.scope_ref.get().to_dict()["limit"] == 3


def test_stale_job_precondition_prevents_slot_assignment(stores):
    from google.api_core.exceptions import FailedPrecondition
    from src.sync_capacity.firestore_store import _ComparedBatch
    a, b = stores
    item = put(a, 1)
    ref = a.jobs.document(item.job_id)
    batch = _ComparedBatch(a.client)
    scope = a._scope(batch)
    batch.get(ref)
    scope["slots"]["0"] = {"owner": "first", "job_id": item.job_id}
    batch.update(a.scope_ref, {"slots": scope["slots"]})
    batch.update(ref, {"state": "processing", "owner": "first"})
    b.jobs.document(item.job_id).update({"state": "needs_attention"})
    with pytest.raises(FailedPrecondition):
        batch.commit()
    assert all(value is None for value in a.scope_ref.get().to_dict()["slots"].values())
    assert ref.get().to_dict()["state"] == "needs_attention"


def test_committed_claim_response_loss_retains_job_and_slot(stores, monkeypatch):
    from google.cloud.firestore_v1.batch import WriteBatch
    a, b = stores
    item = put(a, 1)
    original = WriteBatch.commit
    calls = []
    def lost_response(self, **kwargs):
        calls.append(kwargs)
        original(self, **kwargs)
        raise TimeoutError("response lost after commit")
    monkeypatch.setattr(WriteBatch, "commit", lost_response)
    with pytest.raises(TimeoutError):
        a.claim("uncertain-owner", 10)
    assert len(calls) == 1 and calls[0]["retry"] is None
    data = b.jobs.document(item.job_id).get().to_dict()
    assert data["state"] == "processing"
    assert b.scope_ref.get().to_dict()["slots"][data["slot"]] == {
        "owner": "uncertain-owner", "job_id": item.job_id}


def test_notion_retry_attempt_is_deduplicated_but_changed_data_conflicts(stores):
    a, b = stores
    first = submission("notion", {"id": "attempt-event", "attempt_number": 1, "data": {"value": 1}}, None, 1)
    retry = submission("notion", {"id": "attempt-event", "attempt_number": 2, "data": {"value": 1}}, None, 2)
    changed = submission("notion", {"id": "attempt-event", "attempt_number": 2, "data": {"value": 2}}, None, 3)
    assert a.enqueue(first, 1) == b.enqueue(retry, 2) == "pending"
    with pytest.raises(PayloadConflict):
        b.enqueue(changed, 3)
    assert a.jobs.document(first.job_id).get().to_dict()["created_at"] == 1
