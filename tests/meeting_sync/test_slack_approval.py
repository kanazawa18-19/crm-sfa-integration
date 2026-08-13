from __future__ import annotations

import json
from typing import Any

import pytest

from src.meeting_sync.slack_approval import (
    APPROVE_ACTION_ID,
    REJECT_ACTION_ID,
    MeetingCandidate,
    handle_interaction,
    post_approval_request,
)

_SLACK_API = "https://slack.com/api"


class FakeActionClient:
    def __init__(self, existing_pages: list[dict[str, Any]] | None = None) -> None:
        self.existing_pages = existing_pages or []
        self.created: list[dict[str, Any]] = []
        self.last_filter: dict[str, Any] | None = None

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.last_filter = filter
        return self.existing_pages

    def create_page(self, properties: dict[str, Any]) -> str:
        self.created.append(properties)
        return "new-action-page-1"


def _candidate(**overrides: Any) -> MeetingCandidate:
    base = dict(
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
    base.update(overrides)
    return MeetingCandidate(**base)


@pytest.fixture(autouse=True)
def _slack_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.delenv("SLACK_WEBHOOK_URL_ALERT", raising=False)


# --- post_approval_request: DM解決とメッセージ送信 -----------------------------------------


def test_post_approval_request_does_nothing_when_token_unset(
    monkeypatch: pytest.MonkeyPatch, requests_mock
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    result = post_approval_request(_candidate())

    assert result is False
    assert requests_mock.call_count == 0


def test_post_approval_request_sends_dm_to_resolved_user(requests_mock) -> None:
    requests_mock.get(
        f"{_SLACK_API}/users.lookupByEmail",
        json={"ok": True, "user": {"id": "U123"}},
    )
    requests_mock.post(
        f"{_SLACK_API}/conversations.open",
        json={"ok": True, "channel": {"id": "D123"}},
    )
    post_message = requests_mock.post(f"{_SLACK_API}/chat.postMessage", json={"ok": True})

    result = post_approval_request(_candidate())

    assert result is True
    assert post_message.call_count == 1
    sent_body = post_message.last_request.json()
    assert sent_body["channel"] == "D123"
    actions_block = sent_body["blocks"][1]["elements"]
    assert actions_block[0]["action_id"] == APPROVE_ACTION_ID
    assert actions_block[1]["action_id"] == REJECT_ACTION_ID
    # 実際に解決したSlackユーザーIDがボタンのvalueへ埋め込まれる（handle_interactionの
    # 本人確認で使う）。
    button_value = actions_block[0]["value"]
    assert MeetingCandidate.from_button_value(button_value).rep_slack_user_id == "U123"


def test_post_approval_request_dm_shows_document_url_when_present(requests_mock) -> None:
    requests_mock.get(
        f"{_SLACK_API}/users.lookupByEmail", json={"ok": True, "user": {"id": "U123"}}
    )
    requests_mock.post(
        f"{_SLACK_API}/conversations.open", json={"ok": True, "channel": {"id": "D123"}}
    )
    post_message = requests_mock.post(f"{_SLACK_API}/chat.postMessage", json={"ok": True})

    post_approval_request(_candidate(document_url="https://docs.google.com/document/d/xxxx"))

    summary_text = post_message.last_request.json()["blocks"][0]["text"]["text"]
    assert "https://docs.google.com/document/d/xxxx" in summary_text


def test_post_approval_request_dm_shows_not_yet_arrived_when_document_url_absent(
    requests_mock,
) -> None:
    requests_mock.get(
        f"{_SLACK_API}/users.lookupByEmail", json={"ok": True, "user": {"id": "U123"}}
    )
    requests_mock.post(
        f"{_SLACK_API}/conversations.open", json={"ok": True, "channel": {"id": "D123"}}
    )
    post_message = requests_mock.post(f"{_SLACK_API}/chat.postMessage", json={"ok": True})

    post_approval_request(_candidate())

    summary_text = post_message.last_request.json()["blocks"][0]["text"]["text"]
    assert "まだ届いていません" in summary_text


def test_post_approval_request_skips_when_user_lookup_fails(requests_mock) -> None:
    requests_mock.get(
        f"{_SLACK_API}/users.lookupByEmail",
        json={"ok": False, "error": "users_not_found"},
    )

    result = post_approval_request(_candidate())

    # conversations.openやchat.postMessageへのリクエストは一切飛ばない
    assert result is False
    assert requests_mock.call_count == 1


def test_post_approval_request_alerts_ops_channel_when_dm_delivery_fails(
    monkeypatch: pytest.MonkeyPatch, requests_mock
) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.com/services/T000/alert")
    requests_mock.get(
        f"{_SLACK_API}/users.lookupByEmail",
        json={"ok": False, "error": "users_not_found"},
    )
    alert_call = requests_mock.post(
        "https://hooks.slack.com/services/T000/alert", json={"ok": True}
    )

    result = post_approval_request(_candidate())

    assert result is False
    assert alert_call.call_count == 1
    assert "sales@cnctor.jp" in alert_call.last_request.json()["text"]


# --- handle_interaction: 承認/却下ボタン ----------------------------------------------------


def _interaction_payload(
    action_id: str, candidate: MeetingCandidate, *, clicking_user_id: str = "U123"
) -> dict[str, Any]:
    return {
        "response_url": "https://hooks.slack.com/actions/T000/000/xxx",
        "user": {"id": clicking_user_id},
        "actions": [{"action_id": action_id, "value": candidate.to_button_value()}],
    }


def test_handle_interaction_approve_creates_action_page(requests_mock) -> None:
    requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})
    client = FakeActionClient(existing_pages=[])
    candidate = _candidate()

    handle_interaction(_interaction_payload(APPROVE_ACTION_ID, candidate), client)

    assert len(client.created) == 1
    properties = client.created[0]
    assert properties["Googleカレンダーイベントid"] == "event-1"
    assert properties["案件名"] == ["project-1"]
    assert properties["アクション種別"] == "訪問商談"
    assert "議事録・録画リンク" not in properties


def test_handle_interaction_approve_includes_document_url_when_present(requests_mock) -> None:
    requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})
    client = FakeActionClient(existing_pages=[])
    candidate = _candidate(document_url="https://docs.google.com/document/d/xxxx")

    handle_interaction(_interaction_payload(APPROVE_ACTION_ID, candidate), client)

    assert client.created[0]["議事録・録画リンク"] == "https://docs.google.com/document/d/xxxx"


def test_handle_interaction_approve_drops_non_http_document_url(requests_mock) -> None:
    # _build_action_properties()側のdefense-in-depthチェックの確認。to_button_value()を
    # 経由しない形（例えば直接構築されたMeetingCandidate）で不正なdocument_urlが渡っても
    # Notionへは書き込まれない。
    requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})
    client = FakeActionClient(existing_pages=[])
    candidate = _candidate(document_url="not-a-url")

    handle_interaction(_interaction_payload(APPROVE_ACTION_ID, candidate), client)

    assert "議事録・録画リンク" not in client.created[0]


def test_to_button_value_drops_document_url_when_too_long() -> None:
    from src.meeting_sync.slack_approval import _MAX_DOCUMENT_URL_LEN

    long_url = "https://docs.google.com/document/d/" + "x" * _MAX_DOCUMENT_URL_LEN
    candidate = _candidate(document_url=long_url)

    restored = MeetingCandidate.from_button_value(candidate.to_button_value())

    assert restored.document_url is None


def test_meeting_candidate_from_button_value_defaults_document_url_when_absent() -> None:
    # デプロイ前に投稿済みのSlackメッセージ（button valueにdocument_urlを含まない）を
    # 承認してもKeyErrorにならないことの確認。
    payload = json.loads(_candidate().to_button_value())
    del payload["document_url"]
    stale_value = json.dumps(payload)

    restored = MeetingCandidate.from_button_value(stale_value)
    assert restored.document_url is None


def test_handle_interaction_approve_skips_when_already_registered(requests_mock) -> None:
    requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})
    existing_page = {
        "id": "existing-page",
        "properties": {
            "Googleカレンダーイベントid": {"type": "rich_text", "rich_text": [{"plain_text": "event-1"}]}
        },
    }
    client = FakeActionClient(existing_pages=[existing_page])
    candidate = _candidate()

    handle_interaction(_interaction_payload(APPROVE_ACTION_ID, candidate), client)

    assert client.created == []


def test_handle_interaction_reject_does_not_create_page(requests_mock) -> None:
    requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})
    client = FakeActionClient(existing_pages=[])
    candidate = _candidate()

    handle_interaction(_interaction_payload(REJECT_ACTION_ID, candidate), client)

    assert client.created == []


def test_handle_interaction_updates_original_message_via_response_url(requests_mock) -> None:
    update_call = requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})
    client = FakeActionClient(existing_pages=[])
    candidate = _candidate()

    handle_interaction(_interaction_payload(APPROVE_ACTION_ID, candidate), client)

    assert update_call.call_count == 1
    assert update_call.last_request.json()["replace_original"] is True


def test_handle_interaction_rejects_when_clicking_user_does_not_match_rep(requests_mock) -> None:
    requests_mock.post("https://hooks.slack.com/actions/T000/000/xxx", json={})
    client = FakeActionClient(existing_pages=[])
    candidate = _candidate(rep_slack_user_id="U123")

    handle_interaction(
        _interaction_payload(APPROVE_ACTION_ID, candidate, clicking_user_id="U999"), client
    )

    assert client.created == []
