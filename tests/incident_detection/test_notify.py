"""インシデント検知Slack通知(`src.incident_detection.notify`)の単体テスト。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.incident_detection import notify


def test_notify_managers_immediate_skips_when_webhook_url_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL_ALERT", raising=False)
    calls: list[Any] = []
    monkeypatch.setattr(notify.requests, "post", lambda *args, **kwargs: calls.append((args, kwargs)))

    notify.notify_managers_immediate(
        subject="件名", snippet="本文", contact_email="lead@client.example.com", rep_email="rep@cnctor.jp", score=10
    )

    assert calls == []


def test_notify_managers_immediate_posts_expected_content_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.com/services/xxx")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda url, json, timeout: calls.append({"url": url, "json": json, "timeout": timeout}),
    )

    notify.notify_managers_immediate(
        subject="至急ご確認ください",
        snippet="不具合が発生しております",
        contact_email="lead@client.example.com",
        rep_email="rep@cnctor.jp",
        score=10,
    )

    assert len(calls) == 1
    assert calls[0]["url"] == "https://hooks.slack.com/services/xxx"
    text = calls[0]["json"]["text"]
    assert "lead@client.example.com" in text
    assert "rep@cnctor.jp" in text
    assert "10" in text
    assert "至急ご確認ください" in text


def test_notify_managers_immediate_does_not_raise_when_post_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.com/services/xxx")

    def fail_post(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("network error")

    monkeypatch.setattr(notify.requests, "post", fail_post)

    # 例外を送出せず静かに失敗を吸収する(メイン処理を止めない設計)。
    notify.notify_managers_immediate(
        subject="件名", snippet=None, contact_email="lead@client.example.com", rep_email="rep@cnctor.jp", score=9
    )


def test_run_incident_digest_skips_slack_post_when_no_medium_priority_emails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify.db, "find_medium_priority_since", lambda since: [])
    calls: list[Any] = []
    monkeypatch.setattr(notify.requests, "post", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = notify.run_incident_digest()

    assert result == {"count": 0}
    assert calls == []


def test_run_incident_digest_posts_single_summary_message_when_medium_priority_emails_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        {
            "id": "log-1",
            "contactEmail": "lead1@client.example.com",
            "repEmail": "rep1@cnctor.jp",
            "subject": "対応状況について",
            "incidentScore": 5,
            "sentAt": datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc),
        },
        {
            "id": "log-2",
            "contactEmail": "lead2@client.example.com",
            "repEmail": "rep2@cnctor.jp",
            "subject": None,
            "incidentScore": 6,
            "sentAt": datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc),
        },
    ]
    monkeypatch.setattr(notify.db, "find_medium_priority_since", lambda since: rows)
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.com/services/xxx")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        notify.requests,
        "post",
        lambda url, json, timeout: calls.append({"url": url, "json": json, "timeout": timeout}),
    )

    result = notify.run_incident_digest()

    assert result == {"count": 2}
    assert len(calls) == 1
    text = calls[0]["json"]["text"]
    assert "2件" in text
    assert "lead1@client.example.com" in text
    assert "lead2@client.example.com" in text


def test_run_incident_digest_skips_slack_post_when_webhook_url_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notify.db,
        "find_medium_priority_since",
        lambda since: [
            {
                "id": "log-1",
                "contactEmail": "lead1@client.example.com",
                "repEmail": "rep1@cnctor.jp",
                "subject": "対応状況について",
                "incidentScore": 5,
                "sentAt": datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc),
            }
        ],
    )
    monkeypatch.delenv("SLACK_WEBHOOK_URL_ALERT", raising=False)
    calls: list[Any] = []
    monkeypatch.setattr(notify.requests, "post", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = notify.run_incident_digest()

    assert result == {"count": 1}
    assert calls == []
