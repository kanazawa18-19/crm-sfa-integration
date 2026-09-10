"""受付がDBに触れないことと、処理途中の失敗を再送しないこと。"""

import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

from src.sync_capacity.application import drain_one
from src.sync_capacity.domain import Claim, submission, result_state
from src.sync_capacity.http import CapacityMiddleware
from src.sync_capacity.worker import ObservedDispatcher


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setenv("SYNC_CAPACITY_ENABLED", "true")
    for source in ("NOTION", "KINTONE", "ZOHO", "SPREADSHEET"):
        monkeypatch.setenv(f"{source}_WEBHOOK_SECRET", "test-only-secret")
    monkeypatch.setenv("CRON_SECRET", "test-only-secret")
    app = FastAPI()
    dependency = Mock(side_effect=AssertionError("DB wiring reached"))
    def wiring():
        return dependency()
    for source in ("notion", "kintone", "zoho", "spreadsheet"):
        @app.post(f"/api/webhooks/{source}")
        def endpoint(value=Depends(wiring)):
            return {}
    app.add_middleware(CapacityMiddleware)
    store = Mock()
    store.enqueue.return_value = "pending"
    factory = Mock(return_value=store)
    monkeypatch.setattr("src.sync_capacity.http.get_store", factory)
    return TestClient(app), store, factory, dependency


@pytest.mark.parametrize("source", ["notion", "kintone", "zoho", "spreadsheet"])
def test_unauthorized_never_saves_or_connects(gate, source):
    client, store, factory, db = gate
    response = client.post(f"/api/webhooks/{source}", json={})
    assert response.status_code == 401
    factory.assert_not_called()
    store.enqueue.assert_not_called()
    db.assert_not_called()


@pytest.mark.parametrize("source", ["notion", "kintone", "zoho", "spreadsheet"])
def test_authenticated_acceptance_precedes_wiring(gate, source):
    client, store, _, db = gate
    payload = {"id": "event-1"}
    headers = {}
    path = f"/api/webhooks/{source}"
    if source == "zoho":
        payload["token"] = "test-only-secret"
    if source == "kintone":
        path += "?secret=test-only-secret"
    body = json.dumps(payload)
    if source == "notion":
        headers["X-Notion-Signature"] = "sha256=" + hmac.new(
            b"test-only-secret", body.encode(), hashlib.sha256).hexdigest()
    if source == "spreadsheet":
        headers["X-Webhook-Secret"] = "test-only-secret"
    response = client.post(path, content=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    saved = store.enqueue.call_args.args[0]
    assert "test-only-secret" not in repr(saved)
    assert "query_params" not in saved.event
    db.assert_not_called()


def test_save_failure_does_not_fall_back(gate):
    client, store, _, db = gate
    store.enqueue.side_effect = RuntimeError("sensitive SDK details")
    response = client.post("/api/webhooks/spreadsheet", json={},
                           headers={"X-Webhook-Secret": "test-only-secret"})
    assert response.status_code == 503
    assert "sensitive" not in response.text
    db.assert_not_called()


def test_unsigned_local_flag_is_not_accepted(gate, monkeypatch):
    client, _, factory, _ = gate
    monkeypatch.delenv("SPREADSHEET_WEBHOOK_SECRET")
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "true")
    assert client.post("/api/webhooks/spreadsheet", json={}).status_code == 503
    factory.assert_not_called()


def test_payload_limit_before_store(gate):
    client, _, factory, _ = gate
    response = client.post("/api/webhooks/spreadsheet", content="x" * (256 * 1024 + 1))
    assert response.status_code == 413
    factory.assert_not_called()


def test_outbox_each_invocation_is_retained(gate):
    client, store, _, db = gate
    for _ in range(2):
        response = client.get("/api/cron/spreadsheet-outbox-drain",
                              headers={"Authorization": "Bearer test-only-secret"})
        assert response.status_code == 200
    assert store.enqueue.call_args_list[0].args[0].job_id != store.enqueue.call_args_list[1].args[0].job_id
    db.assert_not_called()


def test_zoho_missing_time_preserves_distinct_notifications():
    a = submission("zoho", {"token": "old", "ids": [1]}, None, 1, receipt_id="receipt-a")
    b = submission("zoho", {"token": "new", "ids": [1]}, None, 2, receipt_id="receipt-b")
    assert a.job_id != b.job_id
    assert a.payload_hash == b.payload_hash
    assert json.loads(a.event["body"])["server_time"] == 1000


def claimed_store():
    store = Mock()
    store.claim.return_value = Claim("job", "owner", "0", "notion", {})
    return store


def test_initialization_failure_retries_without_execution():
    store = claimed_store()
    prepare = Mock(side_effect=RuntimeError())
    assert drain_one(store, prepare)["state"] == "retry"
    assert store.finish.call_args.args[1:3] == ("retry", "initialization_failed")


def test_execution_exception_needs_attention():
    store = claimed_store()
    execute = Mock(side_effect=RuntimeError())
    assert drain_one(store, lambda _: execute)["state"] == "needs_attention"
    assert store.finish.call_args.args[1:3] == ("needs_attention", "execution_failed")


def test_process_exit_retains_claim():
    store = claimed_store()
    execute = Mock(side_effect=SystemExit())
    with pytest.raises(SystemExit):
        drain_one(store, lambda _: execute)
    store.finish.assert_not_called()


def test_claim_response_loss_never_starts_processing():
    store = claimed_store()
    store.claim.side_effect = TimeoutError()
    prepare = Mock()
    with pytest.raises(TimeoutError):
        drain_one(store, prepare)
    prepare.assert_not_called()
    store.finish.assert_not_called()


def test_finish_response_loss_does_not_release_again():
    store = claimed_store()
    store.finish.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        drain_one(store, lambda _: lambda: ({"statusCode": 200, "body": "{}"}, False))
    store.finish.assert_called_once()


@pytest.mark.parametrize("result,partial", [
    ({"statusCode": 500}, False),
    ({"statusCode": 200, "queue_needs_attention": True}, False),
    ({"statusCode": 200, "body": "{}"}, True),
    ({"statusCode": 200, "body": '{"skipped":"sync_bot_id_not_configured"}'}, False),
])
def test_partial_or_failed_is_not_completed(result, partial):
    assert result_state(result, partial=partial)[0] == "needs_attention"


def test_all_dispatch_results_are_observed():
    dispatcher = Mock()
    dispatcher.dispatch.side_effect = [
        SimpleNamespace(has_partial_skips=True, skipped=False, reason=None),
        SimpleNamespace(has_partial_skips=False, skipped=False, reason=None),
    ]
    observed = ObservedDispatcher(dispatcher)
    observed.dispatch({})
    observed.dispatch({})
    assert observed.partial


def test_unknown_new_record_result_needs_attention():
    observed = ObservedDispatcher(Mock(dispatch=Mock(return_value=SimpleNamespace(
        has_partial_skips=False, skipped=True, reason="new_record_creation_status_unknown"))))
    observed.dispatch({})
    assert observed.partial


def test_notion_stale_is_retained_for_field_level_review():
    observed = ObservedDispatcher(Mock(dispatch=Mock(return_value=SimpleNamespace(
        has_partial_skips=False, skipped=True, reason="stale_event"))), source="notion")
    observed.dispatch({})
    assert observed.partial


def test_mistyped_queue_flag_never_reaches_db(gate, monkeypatch):
    client, _, factory, db = gate
    monkeypatch.setenv("SYNC_CAPACITY_ENABLED", "tru")
    assert client.post("/api/webhooks/spreadsheet", json={}).status_code == 503
    factory.assert_not_called()
    db.assert_not_called()


def test_trusted_notion_bypasses_receipt_and_preserves_side_hook_failure(monkeypatch):
    from src.sync_engine.webhook_handlers import notion_webhook as notion
    claim = Mock(side_effect=AssertionError("legacy receipt claim must not run"))
    release = Mock(side_effect=AssertionError("legacy receipt release must not run"))
    monkeypatch.setattr(notion, "claim_event", claim)
    monkeypatch.setattr(notion, "release_event", release)
    monkeypatch.setattr(notion, "is_own_notion_write", lambda *_: False)
    monkeypatch.setattr(notion, "_normalize_fetched_page", lambda *_: {})
    monkeypatch.setattr(notion, "notion_payload_to_sync_event", lambda *_: SimpleNamespace(
        db_key="project", properties={}, external_id="page"))
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", "will-not-be-saved")
    event = {"body": json.dumps({"id": "evt", "entity": {"id": "page"}}), "headers": {}}
    result = notion.handler_with_proxy(event, None, trusted_queue=True,
        notion_client=Mock(get_raw_page=Mock(return_value={})),
        calendar_sync=Mock(side_effect=RuntimeError("failed")))
    assert result["statusCode"] == 200
    assert result["queue_needs_attention"] is True
    assert result_state(result)[0] == "needs_attention"
    claim.assert_not_called()
    release.assert_not_called()

    # 外部bodyに同名フラグがあってもPython内部引数にはならない。
    event["trusted_queue"] = True
    assert notion.handler_with_proxy(event, None, notion_client=Mock())["statusCode"] == 401


def test_trusted_notion_fetch_failure_does_not_delete_legacy_receipt(monkeypatch):
    from src.sync_engine.webhook_handlers import notion_webhook as notion
    release = Mock()
    monkeypatch.setattr(notion, "release_event", release)
    result = notion.handler_with_proxy({"body": '{"id":"evt","entity":{"id":"page"}}'},
        None, trusted_queue=True, notion_client=Mock(get_raw_page=Mock(side_effect=RuntimeError())))
    assert result["statusCode"] == 500
    release.assert_not_called()


def test_two_notion_updates_keep_delta_and_retain_second_stale(monkeypatch):
    from src.sync_engine.webhook_handlers import notion_webhook as notion
    monkeypatch.setattr(notion, "is_own_notion_write", lambda *_: False)
    normalized = Mock(return_value={})
    monkeypatch.setattr(notion, "_normalize_fetched_page", normalized)
    monkeypatch.setattr(notion, "notion_payload_to_sync_event", lambda *_: SimpleNamespace(
        db_key="action", properties={}, external_id="page"))
    dispatcher = Mock(dispatch=Mock(side_effect=[
        SimpleNamespace(skipped=False, has_partial_skips=False, reason=None),
        SimpleNamespace(skipped=True, has_partial_skips=False, reason="stale_event"),
    ]))
    states = []
    for field in ("field-a", "field-b"):
        observed = ObservedDispatcher(dispatcher, source="notion")
        result = notion.handler_with_proxy({"body": json.dumps({"id": field,
            "entity": {"id": "page"}, "data": {"updated_properties": [field]}})},
            None, trusted_queue=True, dispatcher=observed,
            notion_client=Mock(get_raw_page=Mock(return_value={"last_edited_time": "same"})))
        states.append(result_state(result, partial=observed.partial)[0])
    assert [call.args[1] for call in normalized.call_args_list] == [["field-a"], ["field-b"]]
    assert states == ["completed", "needs_attention"]



@pytest.fixture
def compared_store(monkeypatch):
    """実SDKのWriteBatchから送るcommitだけを捕捉する。ネット接続なし。"""
    from datetime import datetime, timezone
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore
    from google.cloud.firestore_v1.services.firestore import FirestoreClient
    from google.cloud.firestore_v1.types import CommitResponse
    from src.sync_capacity.firestore_store import FirestoreJobStore, Settings
    client = firestore.Client(project="demo-cas-unit", credentials=AnonymousCredentials())
    store = FirestoreJobStore(Settings("demo-cas-unit", "(default)", "unit", 1), client=client)
    stamp = datetime(2026, 9, 10, tzinfo=timezone.utc)
    scope = firestore.DocumentSnapshot(store.scope_ref, {"version": 1, "limit": 1, "slots": {"0": None}},
                                       True, stamp, stamp, stamp)
    monkeypatch.setattr(store.scope_ref, "get", Mock(return_value=scope))
    commit = Mock(return_value=CommitResponse())
    monkeypatch.setattr(FirestoreClient, "commit", commit)
    return store, commit, stamp


def test_real_sdk_batch_has_both_preconditions_and_no_rpc_retry(compared_store, monkeypatch):
    from google.cloud import firestore
    store, commit, stamp = compared_store
    ref = store.jobs.document("new")
    missing = firestore.DocumentSnapshot(ref, None, False, stamp, None, None)
    monkeypatch.setattr(ref, "get", Mock(return_value=missing))
    def operation(batch):
        store._scope(batch)
        batch.create(ref, {"state": "pending"})
    store._atomic(operation)
    kwargs = commit.call_args.kwargs
    assert kwargs["retry"] is None
    assert kwargs["timeout"] == 10
    writes = kwargs["request"]["writes"]
    assert len(writes) == 2
    assert writes[0].current_document.update_time == stamp
    assert writes[0].update_mask.field_paths == ["version"]
    assert writes[1].current_document.exists is False


@pytest.mark.parametrize("failure_name", ["Aborted", "FailedPrecondition", "AlreadyExists"])
def test_confirmed_comparison_conflict_rereads(compared_store, monkeypatch, failure_name):
    from google.api_core import exceptions
    from google.cloud.firestore_v1.types import CommitResponse
    store, commit, _ = compared_store
    commit.side_effect = [getattr(exceptions, failure_name)("comparison failed"), CommitResponse()]
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.sleep", lambda _: None)
    def operation(batch):
        store._scope(batch)
        batch.update(store.scope_ref, {"version": 1})
    store._atomic(operation)
    assert commit.call_count == 2
    assert store.scope_ref.get.call_count == 2


@pytest.mark.parametrize("failure_name", ["DeadlineExceeded", "ServiceUnavailable", "Unknown"])
def test_unknown_commit_does_not_retry(compared_store, failure_name):
    from google.api_core import exceptions
    store, commit, _ = compared_store
    commit.side_effect = getattr(exceptions, failure_name)("unknown result")
    def operation(batch):
        store._scope(batch)
        batch.update(store.scope_ref, {"version": 1})
    with pytest.raises(getattr(exceptions, failure_name)):
        store._atomic(operation)
    assert commit.call_count == 1


@pytest.mark.parametrize("code,retried", [
    ("ABORTED", True), ("FAILED_PRECONDITION", True), ("ALREADY_EXISTS", True),
    ("UNKNOWN", False), ("DEADLINE_EXCEEDED", False), ("UNAVAILABLE", False),
])
def test_grpc_cause_must_also_be_confirmed_conflict(compared_store, monkeypatch, code, retried):
    import grpc
    from google.api_core.exceptions import Aborted
    from google.cloud.firestore_v1.types import CommitResponse
    store, commit, _ = compared_store
    class RpcFailure(grpc.RpcError):
        def code(self):
            return getattr(grpc.StatusCode, code)
    error = Aborted("outer failure")
    error.__cause__ = RpcFailure()
    commit.side_effect = [error, CommitResponse()]
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.sleep", lambda _: None)
    def operation(batch):
        store._scope(batch)
        batch.update(store.scope_ref, {"version": 1})
    if retried:
        store._atomic(operation)
        assert commit.call_count == 2
    else:
        with pytest.raises(Aborted):
            store._atomic(operation)
        assert commit.call_count == 1


def test_conflict_context_cannot_hide_timeout(compared_store):
    from google.api_core.exceptions import FailedPrecondition
    store, commit, _ = compared_store
    error = FailedPrecondition("outer error")
    error.__context__ = TimeoutError("unknown commit")
    commit.side_effect = error
    def operation(batch):
        store._scope(batch)
        batch.update(store.scope_ref, {"version": 1})
    with pytest.raises(FailedPrecondition):
        store._atomic(operation)
    assert commit.call_count == 1


def test_dry_run_never_commits(compared_store):
    store, commit, _ = compared_store
    store.initialize()
    commit.assert_not_called()


def test_notion_attempt_number_is_not_content_but_data_is():
    first = submission("notion", {"id": "event", "attempt_number": 1, "data": {"value": 1}}, None, 1)
    retry = submission("notion", {"id": "event", "attempt_number": 2, "data": {"value": 1}}, None, 2)
    changed = submission("notion", {"id": "event", "attempt_number": 2, "data": {"value": 2}}, None, 3)
    assert first.job_id == retry.job_id == changed.job_id
    assert first.payload_hash == retry.payload_hash != changed.payload_hash
    assert json.loads(retry.event["body"])["attempt_number"] == 2


def test_notion_unidentified_notification_requires_distinct_receipts():
    with pytest.raises(ValueError):
        submission("notion", {"data": {}}, None, 1)
    a = submission("notion", {"data": {}}, None, 1, receipt_id="first")
    b = submission("notion", {"data": {}}, None, 2, receipt_id="second")
    assert a.job_id != b.job_id
    a = submission("notion", {"timestamp": "2026-09-10", "attempt_number": 1}, None, 1)
    b = submission("notion", {"timestamp": "2026-09-10", "attempt_number": 2}, None, 2)
    assert a.job_id == b.job_id


def test_canonical_size_overflow_returns_413_before_save(gate):
    client, _, factory, db = gate
    response = client.post("/api/webhooks/spreadsheet", json={}, headers={
        "X-Webhook-Secret": "test-only-secret", "X-Sync-System-ID": "x" * (256 * 1024)})
    assert response.status_code == 413
    factory.assert_not_called()
    db.assert_not_called()


def test_submission_value_error_returns_400_but_store_value_error_is_503(gate, monkeypatch):
    client, store, factory, _ = gate
    original = submission
    monkeypatch.setattr("src.sync_capacity.http.submission", Mock(side_effect=ValueError("private detail")))
    response = client.post("/api/webhooks/spreadsheet", json={},
                           headers={"X-Webhook-Secret": "test-only-secret"})
    assert response.status_code == 400 and "private" not in response.text
    factory.assert_not_called()
    monkeypatch.setattr("src.sync_capacity.http.submission", original)
    store.enqueue.side_effect = ValueError("storage config failure")
    response = client.post("/api/webhooks/spreadsheet", json={},
                           headers={"X-Webhook-Secret": "test-only-secret"})
    assert response.status_code == 503


@pytest.mark.parametrize("existing_state", [None, "pending", "completed"])
def test_enqueue_observation_separates_new_and_duplicate(compared_store, monkeypatch, caplog, existing_state):
    from google.cloud import firestore
    from src.sync_capacity.telemetry import logger
    store, commit, stamp = compared_store
    item = submission("kintone", {"id": "event", "private": "PRIVATE"}, None, 1)
    ref = store.jobs.document(item.job_id)
    snapshot = firestore.DocumentSnapshot(ref,
        {"state": existing_state, "payload_hash": item.payload_hash} if existing_state else None,
        existing_state is not None, stamp, stamp, stamp)
    monkeypatch.setattr(store.jobs, "document", Mock(return_value=ref))
    monkeypatch.setattr(ref, "get", Mock(return_value=snapshot))
    logger.addHandler(caplog.handler)
    try:
        assert store.enqueue(item, 1) == (existing_state or "pending")
    finally:
        logger.removeHandler(caplog.handler)
    event = caplog.records[-1].sync_capacity
    assert event["event"] == "enqueue"
    assert event["outcome"] == ("duplicate" if existing_state else "new")
    assert "PRIVATE" not in caplog.text


def test_observe_missing_scope_is_not_empty_queue(compared_store, monkeypatch):
    from google.cloud import firestore
    from src.sync_capacity.domain import CapacityUnavailable
    store, commit, stamp = compared_store
    monkeypatch.setattr(store.scope_ref, "get", Mock(return_value=firestore.DocumentSnapshot(
        store.scope_ref, None, False, stamp, None, None)))
    with pytest.raises(CapacityUnavailable):
        store.observe()
    commit.assert_not_called()


@pytest.mark.parametrize("scenario,status,outcome,known_job", [
    ("flag", 503, "rejected_before_save", False),
    ("auth", 401, "rejected_before_save", False),
    ("accepted", 200, "accepted", True),
    ("factory", 503, "store_unavailable", True),
    ("scope", 503, "scope_unavailable", True),
    ("conflict", 409, "content_conflict", True),
    ("timeout", 503, "save_unconfirmed", True),
    ("value_error", 503, "save_unconfirmed", True),
])
def test_receipt_response_classifies_once_and_correlates(gate, monkeypatch, caplog,
                                                         scenario, status, outcome, known_job):
    import uuid
    from src.sync_capacity.domain import CapacityUnavailable, PayloadConflict
    from src.sync_capacity.telemetry import logger
    client, store, factory, _ = gate
    headers = {"X-Webhook-Secret": "test-only-secret"}
    if scenario == "flag":
        monkeypatch.setenv("SYNC_CAPACITY_ENABLED", "PRIVATE")
    elif scenario == "auth":
        headers = {}
    elif scenario == "factory":
        factory.side_effect = ValueError("PRIVATE")
    elif scenario in {"scope", "conflict", "timeout", "value_error"}:
        error = {"scope": CapacityUnavailable, "conflict": PayloadConflict,
                 "timeout": TimeoutError, "value_error": ValueError}[scenario]
        store.enqueue.side_effect = error("PRIVATE")
    logger.addHandler(caplog.handler)
    try:
        response = client.post("/api/webhooks/spreadsheet", json={"field": "PRIVATE"}, headers=headers)
    finally:
        logger.removeHandler(caplog.handler)
    assert response.status_code == status
    records = [r.sync_capacity for r in caplog.records if hasattr(r, "sync_capacity")]
    assert len(records) == 1
    event = records[0]
    assert event["event"] == "receipt_response"
    assert event["outcome"] == outcome
    assert event["status"] == status
    assert (event["job_id"] is not None) == known_job
    assert uuid.UUID(event["receipt_id"]).hex == event["receipt_id"]
    if store.enqueue.called:
        assert store.enqueue.call_args.kwargs["receipt_id"] == event["receipt_id"]
    assert "PRIVATE" not in caplog.text
    assert "test-only-secret" not in caplog.text


def test_enqueue_scope_failure_is_before_commit_and_correlated(compared_store, monkeypatch, caplog):
    from google.cloud import firestore
    from src.sync_capacity.domain import CapacityUnavailable
    from src.sync_capacity.telemetry import logger
    store, commit, stamp = compared_store
    monkeypatch.setattr(store.scope_ref, "get", Mock(return_value=firestore.DocumentSnapshot(
        store.scope_ref, None, False, stamp, None, None)))
    logger.addHandler(caplog.handler)
    try:
        with pytest.raises(CapacityUnavailable):
            store.enqueue(submission("kintone", {"id": "event"}, None, 1), 1, receipt_id="receipt")
    finally:
        logger.removeHandler(caplog.handler)
    commit.assert_not_called()
    event = caplog.records[-1].sync_capacity
    assert event["event"] == "enqueue_scope_unavailable"
    assert event["receipt_id"] == "receipt"
    assert event["severity"] == "WARNING"


@pytest.mark.parametrize("error", [ValueError, TimeoutError])
def test_enqueue_generic_failure_remains_unknown(compared_store, monkeypatch, caplog, error):
    from src.sync_capacity.telemetry import logger
    store, _, _ = compared_store
    monkeypatch.setattr(store, "_atomic", Mock(side_effect=error("PRIVATE")))
    logger.addHandler(caplog.handler)
    try:
        with pytest.raises(error):
            store.enqueue(submission("kintone", {"id": "event"}, None, 1), 1, receipt_id="receipt")
    finally:
        logger.removeHandler(caplog.handler)
    event = caplog.records[-1].sync_capacity
    assert event["event"] == "enqueue_unconfirmed"
    assert event["receipt_id"] == "receipt"
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize("elapsed,chain,attempts,deadline,limit", [
    (0.125, False, 8, False, True), (3.125, False, 1, True, False),
    (0.125, True, 1, False, False), (3.125, True, 1, True, False),
])
def test_atomic_failure_records_original_retry_exit(compared_store, monkeypatch,
                                                    elapsed, chain, attempts, deadline, limit):
    from google.api_core.exceptions import FailedPrecondition
    from scripts.capacity_trial.diagnostics import failure_record
    store, commit, _ = compared_store
    error = FailedPrecondition("synthetic-secret")
    cause = TimeoutError("synthetic-secret") if chain else None
    error.__cause__ = cause
    commit.side_effect = error
    clock = iter([100.0] + [100.0 + elapsed] * 8)
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.monotonic", lambda: next(clock))
    sleep = Mock()
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.sleep", sleep)
    def operation(batch):
        store._scope(batch)
        batch.update(store.scope_ref, {"version": 1})
    with pytest.raises(FailedPrecondition) as caught:
        store._atomic(operation)
    assert caught.value is error and error.__cause__ is cause
    assert commit.call_count == attempts and sleep.call_count == attempts - 1
    assert failure_record(error)["atomic"] == dict(
        attempts=attempts, elapsed_ms=int(elapsed * 1000), phase="commit",
        non_conflict_chain=chain, attempt_limit=limit, deadline=deadline)



@pytest.mark.parametrize("elapsed,retries", [(2.999, 1), (3.0, 0), (3.181, 0)])
def test_first_slow_conflict_retry_boundary(compared_store, monkeypatch, elapsed, retries):
    """初回競合後なら成功できても、3秒以上で再読取りできない現行挙動を再現。"""
    from google.api_core.exceptions import FailedPrecondition
    from google.cloud.firestore_v1.types import CommitResponse
    store, commit, _ = compared_store
    error = FailedPrecondition("synthetic comparison conflict")
    commit.side_effect = [error, CommitResponse()]
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.monotonic",
                        Mock(side_effect=[0.0, elapsed]))
    sleep = Mock()
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.sleep", sleep)
    def operation(batch):
        store._scope(batch)
        batch.update(store.scope_ref, {"version": 1})
        return "saved"
    if retries:
        assert store._atomic(operation) == "saved"
    else:
        with pytest.raises(FailedPrecondition) as caught:
            store._atomic(operation)
        assert caught.value is error
        assert error.capacity_atomic["deadline"] is True
        assert error.capacity_atomic["attempts"] == 1
    assert commit.call_count == 1 + retries
    assert store.scope_ref.get.call_count == 1 + retries
    assert sleep.call_count == retries


def test_success_after_retry_deadline_is_still_returned(compared_store, monkeypatch):
    """3秒が成功処理の打切り期限ではないことを仮想時計で確認する。"""
    store, _, _ = compared_store
    clock = Mock(return_value=0.0)
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.monotonic", clock)
    def operation(batch):
        store._scope(batch)
        batch.update(store.scope_ref, {"version": 1})
        clock.return_value = 30.0
        return "saved"
    assert store._atomic(operation) == "saved"


def test_atomic_eighth_failure_can_also_exceed_deadline(compared_store, monkeypatch):
    from google.api_core.exceptions import FailedPrecondition
    from scripts.capacity_trial.diagnostics import failure_record
    store, commit, _ = compared_store
    error = FailedPrecondition("synthetic-secret")
    error.capacity_failure_origin = "guard_version_check"
    operation = Mock(side_effect=error)
    clock = iter([100.0] + [100.125] * 7 + [103.125])
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.sleep", lambda _: None)
    with pytest.raises(FailedPrecondition) as caught:
        store._atomic(operation)
    assert caught.value is error and operation.call_count == 8
    commit.assert_not_called()
    record = failure_record(error)
    assert record["origin"] == "guard_version_check"
    assert record["atomic"] == dict(attempts=8, elapsed_ms=3125, phase="operation",
                                   non_conflict_chain=False, attempt_limit=True, deadline=True)
