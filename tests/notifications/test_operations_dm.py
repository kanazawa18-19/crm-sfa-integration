"""運用DMの受理判定と失敗時の安全性。HTTPはすべて隔離する。"""

import traceback

import pytest
import requests

from src.notifications import operations_dm as dm

BASE = "https://slack.com/api"


@pytest.fixture
def slack(monkeypatch, requests_mock):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "synthetic-bot-token")
    requests_mock.get(f"{BASE}/users.lookupByEmail", json={"ok": True, "user": {"id": "U123"}})
    requests_mock.post(f"{BASE}/conversations.open", json={"ok": True, "channel": {"id": "D123"}})
    requests_mock.post(f"{BASE}/chat.postMessage", json={"ok": True})
    return requests_mock


def test_resolves_configured_person_and_posts_only_to_dm(slack):
    dm.send_operations_dm("通知")
    lookup, opened, sent = slack.request_history
    assert lookup.qs == {"email": [dm.OPERATIONS_EMAIL]}
    assert opened.json() == {"users": "U123"}
    assert sent.json()["channel"] == "D123"
    assert sent.json()["text"] == "通知"
    assert sent.json()["unfurl_links"] is False
    assert len(slack.request_history) == 3


@pytest.mark.parametrize("endpoint,method", [
    ("users.lookupByEmail", "GET"), ("conversations.open", "POST"), ("chat.postMessage", "POST")
])
@pytest.mark.parametrize("status,payload", [
    (200, {"ok": False, "error": "secret"}), (200, {"ok": "true"}),
    (200, []), (429, {"ok": True}), (500, {"ok": True}), (302, {"ok": True}),
])
def test_rejects_every_failed_step_without_leaking_body(slack, endpoint, method, status, payload):
    slack.register_uri(method, f"{BASE}/{endpoint}", status_code=status, json=payload,
                       headers={"Location": "https://example.invalid/secret"})
    with pytest.raises(dm.OperationsDMDeliveryError) as error:
        dm.send_operations_dm("通知")
    assert "secret" not in "".join(traceback.format_exception(error.value))
    assert slack.last_request.url == f"{BASE}/{endpoint}" or endpoint == "users.lookupByEmail"


@pytest.mark.parametrize("endpoint,method,payload", [
    ("users.lookupByEmail", "GET", {"ok": True}),
    ("users.lookupByEmail", "GET", {"ok": True, "user": {"id": "invalid"}}),
    ("conversations.open", "POST", {"ok": True}),
    ("conversations.open", "POST", {"ok": True, "channel": {"id": "C123"}}),
])
def test_malformed_recipient_cannot_post(slack, endpoint, method, payload):
    slack.register_uri(method, f"{BASE}/{endpoint}", json=payload)
    with pytest.raises(dm.OperationsDMDeliveryError):
        dm.send_operations_dm("通知")
    assert not any(r.url.endswith("chat.postMessage") for r in slack.request_history)


@pytest.mark.parametrize("exc", [requests.Timeout, requests.ConnectionError, RuntimeError])
def test_transport_failure_is_fixed_and_not_chained(slack, exc):
    slack.get(f"{BASE}/users.lookupByEmail", exc=exc("secret"))
    with pytest.raises(dm.OperationsDMDeliveryError) as error:
        dm.send_operations_dm("通知")
    assert "secret" not in "".join(traceback.format_exception(error.value))


def test_invalid_json_is_not_success(slack):
    slack.post(f"{BASE}/chat.postMessage", text="secret")
    with pytest.raises(dm.OperationsDMDeliveryError) as error:
        dm.send_operations_dm("通知")
    assert "secret" not in "".join(traceback.format_exception(error.value))


def test_missing_token_makes_no_request(slack, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", " ")
    with pytest.raises(dm.OperationsDMDeliveryError):
        dm.send_operations_dm("通知")
    assert slack.call_count == 0


@pytest.mark.parametrize("module_name", ["src.project_mirror.sync", "src.relation_sync.sync"])
@pytest.mark.parametrize("operations_succeeds", [True, False])
def test_mirror_keeps_managers_and_excludes_only_accepted_operations_dm(
    monkeypatch, module_name, operations_succeeds
):
    import importlib
    from src.notifications import manager_dm
    module = importlib.import_module(module_name)
    delivered = []
    monkeypatch.setenv("SLACK_BOT_TOKEN", "synthetic")
    monkeypatch.setattr(manager_dm, "find_manager_emails", lambda: [" KANAZAWA@cnctor.jp ", "other@example.test"])
    monkeypatch.setattr(manager_dm, "send_dm", lambda email, text: delivered.append(email))
    def operations(text):
        if not operations_succeeds:
            raise dm.OperationsDMDeliveryError("受理失敗")
        delivered.append(dm.OPERATIONS_EMAIL)
    monkeypatch.setattr(dm, "send_operations_dm", operations)
    accepted = module._notify_slack_alert("通知")
    module._notify_managers_slack_dm("通知", operations_sent=accepted)
    assert accepted is operations_succeeds
    assert [email.strip().casefold() for email in delivered] == [dm.OPERATIONS_EMAIL, "other@example.test"]


@pytest.mark.parametrize("module_name,function", [
    ("src.project_mirror.sync", "_notify_slack_alert"),
    ("src.relation_sync.sync", "_notify_slack_alert"),
    ("src.api.token_encryption_healthcheck", "_notify_slack_alert"),
])
def test_best_effort_alerts_do_not_expose_exceptions(monkeypatch, caplog, module_name, function):
    import importlib
    def fail(text):
        raise RuntimeError("secret-token")
    monkeypatch.setattr(dm, "send_operations_dm", fail)
    getattr(importlib.import_module(module_name), function)("通知")
    assert "secret-token" not in caplog.text
    assert "DM送信失敗" in caplog.text


@pytest.mark.parametrize("flag", ["deleted", "is_bot"])
def test_inactive_or_bot_recipient_is_rejected(slack, flag):
    slack.get(f"{BASE}/users.lookupByEmail", json={"ok": True, "user": {"id": "U123", flag: True}})
    with pytest.raises(dm.OperationsDMDeliveryError):
        dm.send_operations_dm("通知")
    assert slack.call_count == 1


@pytest.mark.parametrize("endpoint,method,stage", [
    ("users.lookupByEmail", "GET", "本人検索"),
    ("conversations.open", "POST", "DM開始"),
    ("chat.postMessage", "POST", "投稿"),
])
@pytest.mark.parametrize("response,reason", [
    ({"status_code": 429, "text": "secret"}, "HTTP拒否"),
    ({"text": "secret"}, "JSON不正"),
    ({"json": {"ok": False, "error": "secret"}}, "Slack拒否"),
    ({"exc": requests.Timeout("secret")}, "タイムアウト"),
    ({"exc": requests.ConnectionError("secret")}, "接続失敗"),
])
def test_failure_classification_identifies_stage_and_reason(slack, endpoint, method, stage, response, reason):
    slack.register_uri(method, f"{BASE}/{endpoint}", **response)
    with pytest.raises(dm.OperationsDMDeliveryError) as error:
        dm.send_operations_dm("通知")
    assert error.value.stage == stage
    assert error.value.reason == reason
    assert "secret" not in dm.safe_failure_message(error.value)


def test_failure_log_does_not_trust_exception_attributes(caplog):
    import logging
    error = dm.OperationsDMDeliveryError("本人検索", "HTTP拒否")
    error.stage = "secret-stage"
    error.reason = ["secret-reason"]
    error.args = ("secret-message",)
    dm.log_delivery_failure(logging.getLogger(__name__), error)
    dm.log_delivery_failure(logging.getLogger(__name__), RuntimeError("secret"))
    assert "secret" not in caplog.text
    assert "工程: その他、原因: その他" in caplog.text


@pytest.mark.parametrize("module_name,source", [
    ("src.project_mirror.sync", "refresh_all_projects"),
    ("src.project_mirror.sync", "refresh_projects_incrementally"),
    ("src.relation_sync.sync", "refresh_all_client_names"),
    ("src.relation_sync.sync", "refresh_client_names_incrementally"),
])
def test_mirror_failure_logs_identify_full_or_incremental_source(monkeypatch, caplog, module_name, source):
    import importlib
    def fail(text):
        raise dm.OperationsDMDeliveryError("投稿", "HTTP拒否")
    monkeypatch.setattr(dm, "send_operations_dm", fail)
    assert importlib.import_module(module_name)._notify_slack_alert("通知", source=source) is False
    assert f"{source}: 運用DM送信失敗" in caplog.text
    assert "工程: 投稿、原因: HTTP拒否" in caplog.text


@pytest.mark.parametrize("source", ["secret-source", ["secret-source"]])
def test_failure_log_rejects_unknown_source(caplog, source):
    import logging
    dm.log_delivery_failure(logging.getLogger(__name__), RuntimeError("secret-error"), source=source)
    assert "secret" not in caplog.text
    assert "その他: 運用DM送信失敗" in caplog.text
