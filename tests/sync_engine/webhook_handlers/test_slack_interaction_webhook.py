from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import urlencode

import pytest

from src.meeting_sync.slack_approval import APPROVE_ACTION_ID, MeetingCandidate
from src.sync_engine.webhook_handlers.slack_interaction_webhook import handler

_SIGNING_SECRET = "test-signing-secret"


class FakeActionClient:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return []

    def create_page(self, properties: dict[str, Any]) -> str:
        self.created.append(properties)
        return "new-page-1"


def _candidate() -> MeetingCandidate:
    return MeetingCandidate(
        event_id="event-1",
        title="【商談（訪問）】〇〇ホテル様",
        action_type="訪問商談",
        action_date="2026-08-12",
        project_page_id="project-1",
        project_name="〇〇ホテル様導入案件",
        attendee_display="yamada@example.com",
        rep_email="sales@cnctor.jp",
        rep_slack_user_id="U123",
    )


def _signed_event(body: str, *, timestamp: int | None = None) -> dict[str, Any]:
    ts = timestamp if timestamp is not None else int(time.time())
    basestring = f"v0:{ts}:{body}".encode()
    signature = "v0=" + hmac.new(_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
    return {
        "body": body,
        "headers": {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": signature},
    }


def _interaction_body(action_id: str) -> str:
    payload = {
        "response_url": "https://hooks.slack.com/actions/T000/000/xxx",
        "user": {"id": "U123"},
        "actions": [{"action_id": action_id, "value": _candidate().to_button_value()}],
    }
    return urlencode({"payload": json.dumps(payload)})


@pytest.fixture(autouse=True)
def _signing_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_SIGNING_SECRET", _SIGNING_SECRET)


def test_handler_returns_401_when_signature_missing() -> None:
    event = {"body": _interaction_body(APPROVE_ACTION_ID), "headers": {}}

    response = handler(event, context=None, action_client=FakeActionClient())

    assert response["statusCode"] == 401


def test_handler_returns_401_when_signature_invalid() -> None:
    body = _interaction_body(APPROVE_ACTION_ID)
    event = {
        "body": body,
        "headers": {
            "X-Slack-Request-Timestamp": str(int(time.time())),
            "X-Slack-Signature": "v0=deadbeef",
        },
    }

    response = handler(event, context=None, action_client=FakeActionClient())

    assert response["statusCode"] == 401


def test_handler_returns_401_when_signing_secret_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    event = _signed_event(_interaction_body(APPROVE_ACTION_ID))

    response = handler(event, context=None, action_client=FakeActionClient())

    assert response["statusCode"] == 401


def test_handler_returns_401_when_timestamp_too_old() -> None:
    old_timestamp = int(time.time()) - 60 * 10  # 10分前
    event = _signed_event(_interaction_body(APPROVE_ACTION_ID), timestamp=old_timestamp)

    response = handler(event, context=None, action_client=FakeActionClient())

    assert response["statusCode"] == 401


def test_handler_creates_action_page_on_approve(requests_mock) -> None:
    requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})
    client = FakeActionClient()
    event = _signed_event(_interaction_body(APPROVE_ACTION_ID))

    response = handler(event, context=None, action_client=client)

    assert response["statusCode"] == 200
    assert len(client.created) == 1
