from __future__ import annotations

import pytest

from src.email_reminders import slack_notify
from src.email_reminders.slack_notify import SlackDeliveryError


def test_send_reminder_dm_raises_when_slack_bot_token_not_set(monkeypatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    with pytest.raises(SlackDeliveryError):
        slack_notify.send_reminder_dm(
            rep_email="rep@cnctor.jp", contact_email="lead@client.example.com", hours_elapsed=6, subject="件名"
        )


def test_send_reminder_dm_raises_when_dm_channel_resolution_fails(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(slack_notify, "_resolve_dm_channel", lambda rep_email: None)

    with pytest.raises(SlackDeliveryError):
        slack_notify.send_reminder_dm(
            rep_email="rep@cnctor.jp", contact_email="lead@client.example.com", hours_elapsed=6, subject="件名"
        )


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_send_reminder_dm_succeeds_when_chat_post_message_ok(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(slack_notify, "_resolve_dm_channel", lambda rep_email: ("C123", "U123"))

    posted: list[dict] = []
    monkeypatch.setattr(
        slack_notify.requests,
        "post",
        lambda url, headers, json, timeout: posted.append({"url": url, "json": json}) or _FakeResponse({"ok": True}),
    )

    slack_notify.send_reminder_dm(
        rep_email="rep@cnctor.jp", contact_email="lead@client.example.com", hours_elapsed=6, subject="件名"
    )

    assert posted[0]["json"]["channel"] == "C123"


def test_send_reminder_dm_raises_when_chat_post_message_fails(monkeypatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(slack_notify, "_resolve_dm_channel", lambda rep_email: ("C123", "U123"))
    monkeypatch.setattr(
        slack_notify.requests,
        "post",
        lambda url, headers, json, timeout: _FakeResponse({"ok": False, "error": "channel_not_found"}),
    )

    with pytest.raises(SlackDeliveryError):
        slack_notify.send_reminder_dm(
            rep_email="rep@cnctor.jp", contact_email="lead@client.example.com", hours_elapsed=6, subject="件名"
        )
