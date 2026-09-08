"""インシデント検知Slack通知(`src.incident_detection.notify`)の単体テスト。"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
import traceback
from typing import Any

import pytest

from src.incident_detection import notify


class _FakeSlackResponse:
    """`requests.post(...).json()`のダミー戻り値(`tests/email_reminders/test_slack_notify.py`の
    `_FakeResponse`と同じパターン)。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def test_notify_managers_immediate_skips_when_slack_bot_token_not_configured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    resolve_calls: list[str] = []
    monkeypatch.setattr(notify, "_resolve_dm_channel", lambda email: resolve_calls.append(email))
    monkeypatch.setattr(
        notify.db, "find_manager_emails", lambda: (_ for _ in ()).throw(AssertionError("should not be called"))
    )

    with caplog.at_level("WARNING"):
        notify.notify_managers_immediate(
            subject="件名", snippet="本文", contact_email="lead@client.example.com", rep_email="rep@cnctor.jp", score=10
        )

    assert resolve_calls == []
    # manager_dm.notify_managers()と同じ対応(2026-08-25): 未設定時も痕跡がログに残ること。
    assert any("SLACK_BOT_TOKEN is not configured" in r.getMessage() for r in caplog.records)


def test_notify_managers_immediate_skips_when_no_managers_found(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(notify.db, "find_manager_emails", lambda: [])
    resolve_calls: list[str] = []
    monkeypatch.setattr(notify, "_resolve_dm_channel", lambda email: resolve_calls.append(email))

    with caplog.at_level("WARNING"):
        notify.notify_managers_immediate(
            subject="件名", snippet="本文", contact_email="lead@client.example.com", rep_email="rep@cnctor.jp", score=10
        )

    assert resolve_calls == []
    # manager_dm.notify_managers()と同じ対応(2026-08-25): 0人時も痕跡がログに残ること。
    assert any("no managers found" in r.getMessage() for r in caplog.records)


def test_notify_managers_immediate_skips_silently_when_find_manager_emails_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    def fail_find_manager_emails() -> list[str]:
        raise RuntimeError("db connection error")

    monkeypatch.setattr(notify.db, "find_manager_emails", fail_find_manager_emails)

    # 例外を送出せず静かに失敗を吸収する(メイン処理を止めない設計)。
    notify.notify_managers_immediate(
        subject="件名", snippet="本文", contact_email="lead@client.example.com", rep_email="rep@cnctor.jp", score=10
    )


def test_notify_managers_immediate_sends_dm_to_each_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        notify.db, "find_manager_emails", lambda: ["kanazawa@cnctor.jp", "hiramoto@cnctor.jp"]
    )

    resolved_channels = {"kanazawa@cnctor.jp": ("C-KANAZAWA", "U1"), "hiramoto@cnctor.jp": ("C-HIRAMOTO", "U2")}
    monkeypatch.setattr(notify, "_resolve_dm_channel", lambda email: resolved_channels[email])

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda url, headers, json, timeout: calls.append({"url": url, "json": json}) or _FakeSlackResponse(
            {"ok": True}
        ),
    )

    notify.notify_managers_immediate(
        subject="至急ご確認ください",
        snippet="不具合が発生しております",
        contact_email="lead@client.example.com",
        rep_email="rep@cnctor.jp",
        score=10,
    )

    assert len(calls) == 2
    channels_notified = {call["json"]["channel"] for call in calls}
    assert channels_notified == {"C-KANAZAWA", "C-HIRAMOTO"}
    for call in calls:
        text = call["json"]["text"]
        assert "lead@client.example.com" in text
        assert "rep@cnctor.jp" in text
        assert "10" in text
        assert "至急ご確認ください" in text


def test_notify_managers_immediate_continues_to_next_manager_when_one_dm_channel_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        notify.db, "find_manager_emails", lambda: ["kanazawa@cnctor.jp", "hiramoto@cnctor.jp"]
    )

    def fake_resolve(email: str) -> tuple[str, str] | None:
        return None if email == "kanazawa@cnctor.jp" else ("C-HIRAMOTO", "U2")

    monkeypatch.setattr(notify, "_resolve_dm_channel", fake_resolve)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda url, headers, json, timeout: calls.append({"url": url, "json": json}) or _FakeSlackResponse(
            {"ok": True}
        ),
    )

    notify.notify_managers_immediate(
        subject="件名", snippet="本文", contact_email="lead@client.example.com", rep_email="rep@cnctor.jp", score=10
    )

    # 1人目の解決失敗があっても2人目へは送信される
    assert len(calls) == 1
    assert calls[0]["json"]["channel"] == "C-HIRAMOTO"


def test_notify_managers_immediate_continues_to_next_manager_when_one_post_message_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        notify.db, "find_manager_emails", lambda: ["kanazawa@cnctor.jp", "hiramoto@cnctor.jp"]
    )
    resolved_channels = {"kanazawa@cnctor.jp": ("C-KANAZAWA", "U1"), "hiramoto@cnctor.jp": ("C-HIRAMOTO", "U2")}
    monkeypatch.setattr(notify, "_resolve_dm_channel", lambda email: resolved_channels[email])

    calls: list[dict[str, Any]] = []

    def fake_post(url: str, headers: dict, json: dict, timeout: int) -> Any:
        if json["channel"] == "C-KANAZAWA":
            raise RuntimeError("network error")
        calls.append({"url": url, "json": json})
        return _FakeSlackResponse({"ok": True})

    monkeypatch.setattr(notify.requests, "post", fake_post)

    # 例外を送出せず静かに失敗を吸収しつつ、他の対象者への送信は継続する。
    notify.notify_managers_immediate(
        subject="件名", snippet="本文", contact_email="lead@client.example.com", rep_email="rep@cnctor.jp", score=10
    )

    assert len(calls) == 1
    assert calls[0]["json"]["channel"] == "C-HIRAMOTO"


@pytest.fixture
def digest_setup(monkeypatch):
    rows = [{"id": "log-1", "contactEmail": "lead@example.com",
             "repEmail": "rep@example.com", "subject": "対応状況", "incidentScore": 5}]
    state = {"committed": False, "rolled_back": False, "calls": []}

    @contextmanager
    def claim():
        try:
            yield rows
            state["committed"] = True
        except Exception:
            state["rolled_back"] = True
            raise

    def post(*args, **kwargs):
        assert not state["committed"]
        state["calls"].append(kwargs)
        return SimpleNamespace(status_code=200, text="ok")

    monkeypatch.setattr(notify.db, "claim_undigested_medium_priority_emails", claim)
    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://example.invalid/secret")
    return rows, state


def test_digest_success_commits_after_slack(digest_setup):
    rows, state = digest_setup
    assert notify.run_incident_digest() == {"count": 1, "batch_limit_reached": False}
    assert state["committed"]
    assert "lead@example.com" in state["calls"][0]["json"]["text"]
    assert state["calls"][0]["allow_redirects"] is False


def test_digest_empty_does_not_send(digest_setup):
    rows, state = digest_setup
    rows.clear()
    assert notify.run_incident_digest() == {"count": 0, "batch_limit_reached": False}
    assert not state["calls"]


def test_digest_unconfigured_fails_without_claim(monkeypatch, digest_setup):
    rows, state = digest_setup
    monkeypatch.delenv("SLACK_WEBHOOK_URL_ALERT")
    with pytest.raises(notify.IncidentDigestDeliveryError):
        notify.run_incident_digest()
    assert not state["committed"] and not state["calls"]


@pytest.mark.parametrize("status,body", [(400, "invalid_payload"), (429, "rate_limited"),
                                         (500, "error"), (302, "ok"), (200, "error")])
def test_digest_slack_rejection_rolls_back(monkeypatch, digest_setup, status, body):
    rows, state = digest_setup
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k:
                        SimpleNamespace(status_code=status, text=body))
    with pytest.raises(notify.IncidentDigestDeliveryError):
        notify.run_incident_digest()
    assert state["rolled_back"] and not state["committed"]


def test_digest_transport_failure_hides_secret_and_rolls_back(monkeypatch, digest_setup):
    rows, state = digest_setup
    def fail(*args, **kwargs):
        raise notify.requests.Timeout("https://example.invalid/secret")
    monkeypatch.setattr(notify.requests, "post", fail)
    with pytest.raises(notify.IncidentDigestDeliveryError) as error:
        notify.run_incident_digest()
    assert "https://example.invalid/secret" not in "".join(traceback.format_exception(error.value))
    assert state["rolled_back"] and not state["committed"]


def test_digest_large_fields_are_bounded(digest_setup):
    rows, state = digest_setup
    rows[0].update(contactEmail="&" * 10000, repEmail="<" * 10000,
                   subject=">" * 10000)
    rows.extend([dict(rows[0])] * 49)
    assert notify.run_incident_digest() == {"count": 50, "batch_limit_reached": True}
    text = state["calls"][0]["json"]["text"]
    assert len(text) < 20000
    assert "未通知分が残っている可能性" in text


@pytest.mark.parametrize("value,limit,expected", [
    ("A&Bxxxxxxxxxx", 10, "A&amp;Bxxx…"),
    ("abcd&long", 6, "abcd…"),
    ("A&B", 7, "A&amp;B"),
    ("longplainstring", 5, "longp…"),
])
def test_digest_field_preserves_complete_character_references(value, limit, expected):
    assert notify._digest_field(value, limit) == expected
