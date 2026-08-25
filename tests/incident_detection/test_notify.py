"""インシデント検知Slack通知(`src.incident_detection.notify`)の単体テスト。"""

from __future__ import annotations

from datetime import datetime, timezone
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


# run_incident_digest()は`db.claim_undigested_medium_priority_emails()`が対象行の
# claim(digestedAt更新)まで済ませた上で返す設計(2026-08-16、shirokuma-secレビューWARN対応)
# のため、notify.py側のテストではclaim自体の中身(UPDATE...RETURNINGのアトミック性)は
# 検証せず、db.claim_undigested_medium_priority_emails()の戻り値をそのまま信頼して
# Slack投稿ロジックのみを検証する(claim自体の検証はraw SQLを叩くdb.pyの実装であり、
# 他モジュール同様このリポジトリでは単体テスト対象外としている)。


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


def test_run_incident_digest_skips_slack_post_when_no_medium_priority_emails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notify.db, "claim_undigested_medium_priority_emails", lambda: [])
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
    monkeypatch.setattr(notify.db, "claim_undigested_medium_priority_emails", lambda: rows)
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
        "claim_undigested_medium_priority_emails",
        lambda: [
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


def test_run_incident_digest_does_not_raise_and_still_counts_claimed_rows_when_slack_post_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # claim(digestedAt更新)はSlack投稿より先にコミット済みのため、投稿自体が失敗しても
    # 「claimしたが送信できなかった」件数として結果に反映される(claimされた行は次回の
    # ダイジェストから漏れる、という設計上のトレードオフをテストで固定する)。
    rows = [
        {
            "id": "log-1",
            "contactEmail": "lead1@client.example.com",
            "repEmail": "rep1@cnctor.jp",
            "subject": "対応状況について",
            "incidentScore": 5,
            "sentAt": datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc),
        }
    ]
    monkeypatch.setattr(notify.db, "claim_undigested_medium_priority_emails", lambda: rows)
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.com/services/xxx")

    def fail_post(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("network error")

    monkeypatch.setattr(notify.requests, "post", fail_post)

    result = notify.run_incident_digest()

    assert result == {"count": 1}
