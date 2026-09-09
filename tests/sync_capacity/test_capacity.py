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


def test_only_definite_transaction_abort_is_retried(monkeypatch):
    from google.api_core.exceptions import Aborted
    from google.cloud import firestore
    from src.sync_capacity.firestore_store import FirestoreJobStore
    store = object.__new__(FirestoreJobStore)
    store.client = Mock()
    wrapped = ValueError("SDK exhausted")
    wrapped.__cause__ = Aborted("contention")
    execute = Mock(side_effect=[wrapped, "ok"])
    monkeypatch.setattr(firestore, "transactional", lambda _: execute)
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.sleep", lambda _: None)
    assert store._atomic(lambda _: None) == "ok"
    assert execute.call_count == 2

    for failure in (TimeoutError("unknown commit"), ValueError("not an abort")):
        execute.reset_mock(side_effect=True)
        execute.side_effect = failure
        with pytest.raises(type(failure)):
            store._atomic(lambda _: None)
        assert execute.call_count == 1


def test_rollback_abort_does_not_hide_unknown_commit(monkeypatch):
    from google.api_core.exceptions import Aborted
    from google.cloud import firestore
    from src.sync_capacity.firestore_store import FirestoreJobStore
    store = object.__new__(FirestoreJobStore)
    store.client = Mock()
    rollback_error = Aborted("rollback failed")
    rollback_error.__context__ = TimeoutError("commit unknown")
    execute = Mock(side_effect=rollback_error)
    monkeypatch.setattr(firestore, "transactional", lambda _: execute)
    with pytest.raises(Aborted):
        store._atomic(lambda _: None)
    assert execute.call_count == 1


@pytest.mark.parametrize("code, retried", [
    ("ABORTED", True), ("UNKNOWN", False), ("DEADLINE_EXCEEDED", False),
    ("UNAVAILABLE", False),
])
def test_sdk_grpc_cause_only_retries_confirmed_abort(monkeypatch, code, retried):
    """SDKの例外変換を通し、生gRPC原因を伴う実際の例外構造で検証する。"""
    import grpc
    from google.api_core import grpc_helpers
    from google.api_core.exceptions import Aborted
    from google.cloud import firestore
    from src.sync_capacity.firestore_store import FirestoreJobStore

    class RpcFailure(grpc.RpcError):
        def code(self):
            return getattr(grpc.StatusCode, code)

        def details(self):
            return "合成通信エラー"

        def trailing_metadata(self):
            return None

    def fail_rpc():
        raise RpcFailure()

    try:
        grpc_helpers._wrap_unary_errors(fail_rpc)()
    except Exception as sdk_error:
        assert sdk_error.__cause__ is not None
        assert isinstance(sdk_error.__cause__, grpc.RpcError)
        wrapped = Aborted("rollback aborted")
        # 実SDKのcommit失敗後、rollbackもABORTEDになる場合を含める。
        wrapped.__context__ = sdk_error

    store = object.__new__(FirestoreJobStore)
    store.client = Mock()
    execute = Mock(side_effect=[wrapped, "ok"])
    monkeypatch.setattr(firestore, "transactional", lambda _: execute)
    monkeypatch.setattr("src.sync_capacity.firestore_store.time.sleep", lambda _: None)
    if retried:
        assert store._atomic(lambda _: None) == "ok"
        assert execute.call_count == 2
    else:
        with pytest.raises(Aborted):
            store._atomic(lambda _: None)
        assert execute.call_count == 1
