"""Slack携帯画面の認可・確認・再送・Notion競合を検証する。"""
from __future__ import annotations

import json
import hashlib
import hmac
import time
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from src.slack_crm import domain, handler, service, views
from src.sync_engine.webhook_handlers.slack_interaction_webhook import _verify_slack_signature
from src.db_schema.base import Tool
from src.sync_engine.dispatcher import DispatchResult, PropertyDispatchResult


def _event(payload: dict) -> dict:
    return {"headers": {"Content-Type": "application/x-www-form-urlencoded"},
            "body": urlencode({"payload": json.dumps(payload, ensure_ascii=False)})}


@pytest.fixture(autouse=True)
def _auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_verify_slack_signature", lambda headers, body: True)
    monkeypatch.setenv("SLACK_CRM_ALLOWED_USER_IDS", "U_SELF")
    monkeypatch.setenv("SLACK_CRM_ALLOWED_TEAM_ID", "T_ONE")


def _base(**overrides: object) -> dict:
    value = {"type": "block_actions", "team": {"id": "T_ONE"}, "user": {"id": "U_SELF"}}
    value.update(overrides)
    return value


def test_home_is_private_and_uses_two_large_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(handler, "call", lambda method, body: sent.append((method, body)))
    monkeypatch.setattr(handler.storage, "recent_projects", lambda user_id: [])
    body = json.dumps({"type": "event_callback", "team_id": "T_ONE",
                       "event": {"type": "app_home_opened", "user": "U_SELF"}})
    result = handler.handler({"headers": {"Content-Type": "application/json"}, "body": body})
    assert result["statusCode"] == 200
    assert result["_background_home_user"] == "U_SELF"
    handler.publish_home(result["_background_home_user"])
    assert sent[0][0] == "views.publish"
    assert sent[0][1]["user_id"] == "U_SELF"
    assert len(sent[0][1]["view"]["blocks"][1]["elements"]) == 2
    sent.clear()
    body = json.dumps({"type": "event_callback", "team_id": "T_ONE",
                       "event": {"type": "app_home_opened", "user": "U_OTHER"}})
    handler.handler({"headers": {"Content-Type": "application/json"}, "body": body})
    assert sent == []


def test_project_search_and_other_user_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler.storage, "search_projects", lambda query: [
        {"id": "01234567-89ab-cdef-0123-456789abcdef", "案件名": "試験案件", "営業ステータス": "商談中"}
    ])
    payload = _base(type="block_suggestion", action_id="crm_project_options", value="試験")
    result = handler.handler(_event(payload))
    assert json.loads(result["body"])["options"][0]["value"].startswith("01234567")
    payload["user"]["id"] = "U_OTHER"
    assert json.loads(handler.handler(_event(payload))["body"]) == {}


def test_signed_url_verification_is_not_blocked_by_missing_user() -> None:
    body = json.dumps({"type": "url_verification", "team_id": "T_ONE", "challenge": "challenge-value"})
    result = handler.handler({"headers": {"Content-Type": "application/json"}, "body": body})
    assert json.loads(result["body"])["challenge"] == "challenge-value"
    other = json.dumps({"type": "url_verification", "team_id": "T_OTHER", "challenge": "x"})
    assert json.loads(handler.handler({"headers": {"Content-Type": "application/json"}, "body": other})["body"]) == {}


def test_real_signature_rejects_invalid_and_expired_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handler, "_verify_slack_signature", _verify_slack_signature)
    monkeypatch.setenv("SLACK_SIGNING_SECRET", "unit-test-signing-key")
    body = json.dumps({"type": "url_verification", "team_id": "T_ONE", "challenge": "ok"})
    timestamp = str(int(time.time()))
    signature = "v0=" + hmac.new(b"unit-test-signing-key", f"v0:{timestamp}:{body}".encode(), hashlib.sha256).hexdigest()
    event = {"headers": {"Content-Type": "application/json", "X-Slack-Request-Timestamp": timestamp,
                         "X-Slack-Signature": signature}, "body": body}
    assert json.loads(handler.handler(event)["body"])["challenge"] == "ok"
    event["headers"]["X-Slack-Signature"] = "v0=invalid"
    assert handler.handler(event)["statusCode"] == 401
    event["headers"]["X-Slack-Signature"] = signature
    event["headers"]["X-Slack-Request-Timestamp"] = str(int(time.time()) - 600)
    assert handler.handler(event)["statusCode"] == 401


def test_project_submit_shows_changes_before_saving() -> None:
    pid = "01234567-89ab-cdef-0123-456789abcdef"
    payload = _base(type="view_submission", view={
        "callback_id": "crm_submit_project",
        "private_metadata": json.dumps({"project_id": pid, "expected": {
            "営業ステータス": "商談中", "次回アクション日": "2026-10-03", "確度": "C"}}),
        "state": {"values": {
            "status": {"status_value": {"selected_option": {"value": "見積もり提出済み"}}},
            "date": {"next_date": {"selected_date": "2026-10-03"}},
            "confidence": {"confidence_value": {"selected_option": {"value": "C"}}},
        }},
    })
    result = json.loads(handler.handler(_event(payload))["body"])
    assert result["response_action"] == "push"
    meta = json.loads(result["view"]["private_metadata"])
    assert meta["changes"] == {"営業ステータス": "見積もり提出済み"}
    assert meta["expected"] == {"営業ステータス": "商談中"}


def test_confirm_requires_actor_and_feature_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[tuple[str, dict]] = []
    queued: list[dict] = []
    monkeypatch.setattr(handler, "call", lambda method, body: sent.append((method, body)))
    monkeypatch.setattr(handler.storage, "enqueue", lambda **kw: queued.append(kw) or True)
    pid = "01234567-89ab-cdef-0123-456789abcdef"
    meta = {"actor_id": "U_SELF", "operation_id": "op1", "kind": "project_update",
            "target_id": pid, "expected": {"確度": "C"}, "changes": {"確度": "B"},
            "issued_at": int(time.time())}
    payload = _base(type="view_submission",
                    view={"id": "V1", "callback_id": "crm_preview",
                          "private_metadata": json.dumps(meta)})
    assert "_background_operation_id" not in handler.handler(_event(payload))
    assert queued == []
    monkeypatch.setenv("SLACK_CRM_WRITE_ENABLED", "true")
    meta["actor_id"] = "U_OTHER"
    payload["view"]["private_metadata"] = json.dumps(meta)
    assert "_background_operation_id" not in handler.handler(_event(payload))
    assert queued == []
    meta["actor_id"] = "U_SELF"
    payload["view"]["private_metadata"] = json.dumps(meta)
    result = handler.handler(_event(payload))
    assert result["_background_operation_id"] == "op1"
    assert queued[0]["target_id"] == pid


def test_same_action_form_reuses_operation_id_and_title() -> None:
    pid = "01234567-89ab-cdef-0123-456789abcdef"
    form = views.action_form(pid, project_name="試験案件")
    meta = json.loads(form["private_metadata"])
    payload = _base(type="view_submission", view={
        "callback_id": "crm_submit_action_create", "private_metadata": form["private_metadata"],
        "state": {"values": {
            "action_type": {"type_value": {"selected_option": {"value": "メール"}}},
            "date": {"action_date": {"selected_date": "2026-10-02"}},
            "memo": {"memo_value": {"value": "資料送付"}},
        }},
    })
    first = json.loads(handler.handler(_event(payload))["body"])
    second = json.loads(handler.handler(_event(payload))["body"])
    assert first["response_action"] == "push"
    assert json.loads(first["view"]["private_metadata"])["operation_id"] == meta["operation_id"]
    assert first["view"] == second["view"] or (
        json.loads(first["view"]["private_metadata"])["changes"] ==
        json.loads(second["view"]["private_metadata"])["changes"])
    assert views.receipt_view("受付済み")["clear_on_close"] is True


def test_project_date_can_be_cleared_without_changing_other_fields() -> None:
    pid = "01234567-89ab-cdef-0123-456789abcdef"
    payload = _base(type="view_submission", view={
        "callback_id": "crm_submit_project",
        "private_metadata": json.dumps({"project_id": pid, "expected": {
            "営業ステータス": "商談中", "次回アクション日": "2026-10-03", "確度": "C"}}),
        "state": {"values": {
            "status": {"status_value": {"selected_option": {"value": "商談中"}}},
            "date": {"next_date": {"selected_date": None}},
            "confidence": {"confidence_value": {"selected_option": {"value": "C"}}},
        }},
    })
    response = json.loads(handler.handler(_event(payload))["body"])
    meta = json.loads(response["view"]["private_metadata"])
    assert meta["changes"] == {"次回アクション日": None}


def test_long_existing_memo_is_not_offered_for_short_form_update() -> None:
    form = views.action_form("project-id", action_id="action-id", current={
        "アクション種別": "メール", "アクション日": "2026-10-02", "履歴メモ": "長" * 501})
    assert not any(block.get("block_id") == "memo" for block in form["blocks"])
    assert "履歴メモ" not in json.loads(form["private_metadata"])["expected"]


def test_domain_disallows_terminal_status_and_unlisted_fields() -> None:
    with pytest.raises(ValueError):
        domain.validate_changes("project_update", {"営業ステータス": "契約"})
    with pytest.raises(ValueError):
        domain.validate_changes("project_update", {"案件名": "改名"})
    assert domain.validate_changes("action_update", {"履歴メモ": "確認"}) == {"履歴メモ": "確認"}


class _Notion:
    def __init__(self, current: dict):
        self.current = current
        self.writes: list[dict] = []

    def get_page(self, page_id: str) -> dict:
        return dict(self.current)

    def update_page(self, page_id: str, changes: dict) -> None:
        self.writes.append(changes)
        self.current.update(changes)


def test_worker_stops_stale_value_and_accepts_repeated_result() -> None:
    op = {"kind": "project_update", "targetId": "id", "expected": {"確度": "C"},
          "changes": {"確度": "B"}}
    notion = _Notion({"確度": "A"})
    assert service._state_for_update(notion, op) == ("conflict", "value_changed")
    assert notion.writes == []
    notion.current["確度"] = "C"
    assert service._state_for_update(notion, op) == ("done", None)
    assert notion.writes == [{"確度": "B"}]
    assert service._state_for_update(notion, op) == ("done", None)
    assert len(notion.writes) == 1


def test_action_update_rejects_move_to_another_project() -> None:
    op = {"kind": "action_update", "targetId": "action", "expected": {
        "履歴メモ": "旧", "案件名": ["project-a"]}, "changes": {"履歴メモ": "新"}}
    notion = _Notion({"履歴メモ": "旧", "案件名": ["project-b"]})
    assert service._state_for_update(notion, op) == ("conflict", "relation_changed")
    assert notion.writes == []


def test_partial_sync_is_reported_without_overwriting_newer_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sync_engine import production_wiring
    pid = "01234567-89ab-cdef-0123-456789abcdef"
    operation = {"id": "op", "kind": "project_update", "targetId": pid, "resultPageId": None,
                 "changes": {"確度": "B"}, "syncAttempts": 1, "syncError": None}
    monkeypatch.setattr(service.storage, "claim_sync", lambda op_id=None: operation)
    finished: list[dict] = []
    monkeypatch.setattr(service.storage, "finish_sync", lambda *a, **kw: finished.append(kw))
    monkeypatch.setattr(service, "_client", lambda kind: SimpleNamespace(
        get_page=lambda page_id: {"確度": "B"},
        get_raw_page=lambda page_id: {"last_edited_time": "2026-10-02T00:00:00Z"}))
    dispatched = []
    result = DispatchResult(skipped=False, properties=(PropertyDispatchResult(
        "確度", None, skipped_tools=frozenset({Tool.ZOHO})),))
    wiring = SimpleNamespace(dispatcher=SimpleNamespace(dispatch=lambda event: dispatched.append(event) or result),
                             project_mirror_sync_callable=None, calendar_sync_callable=None)
    monkeypatch.setattr(production_wiring, "get_production_wiring", lambda: wiring)
    assert service.process_sync("op") == {"id": "op", "state": "partial"}
    assert finished == [{"state": "partial", "error": "partial_skip"}]
    assert dispatched[0].occurred_at.isoformat() == "2026-10-02T00:00:00+00:00"


def test_mirror_failure_retries_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sync_engine import production_wiring
    pid = "01234567-89ab-cdef-0123-456789abcdef"
    operation = {"id": "op", "kind": "project_update", "targetId": pid, "resultPageId": None,
                 "changes": {"確度": "B"}, "syncAttempts": 1, "syncError": None}
    monkeypatch.setattr(service.storage, "claim_sync", lambda op_id=None: operation)
    finished: list[dict] = []
    monkeypatch.setattr(service.storage, "finish_sync", lambda *a, **kw: finished.append(kw))
    monkeypatch.setattr(service, "_client", lambda kind: SimpleNamespace(
        get_page=lambda page_id: {"確度": "B"},
        get_raw_page=lambda page_id: {"last_edited_time": "2026-10-02T00:00:00Z"}))
    calls = []
    def mirror(_props, _page_id):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary")
    dispatched = []
    wiring = SimpleNamespace(dispatcher=SimpleNamespace(dispatch=lambda event: dispatched.append(event) or DispatchResult(skipped=False)),
                             project_mirror_sync_callable=mirror, calendar_sync_callable=None)
    monkeypatch.setattr(production_wiring, "get_production_wiring", lambda: wiring)
    assert service.process_sync("op")["state"] == "queued"
    operation["syncAttempts"] = 2
    assert service.process_sync("op")["state"] == "done"
    assert len(calls) == 2
    assert len(dispatched) == 1
    assert finished[-1] == {"state": "done"}


def test_mobile_forms_have_prefilled_values_and_confirmation() -> None:
    project = {"id": "p", "案件名": "試験案件", "営業ステータス": "商談中",
               "次回アクション日": "2026-10-03", "確度": "C"}
    form = views.project_form(project)
    date_block = next(block for block in form["blocks"] if block.get("block_id") == "date")
    assert date_block["element"]["initial_date"] == "2026-10-03"
    assert form["submit"]["text"] == "確認へ"
