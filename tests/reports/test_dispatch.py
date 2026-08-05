"""07_日報週報仕様「配信先」の抽象化（WebhookSlackReportNotifier等）の単体テスト。"""

from __future__ import annotations

from typing import Any

import pytest

from src.reports.dispatch import (
    ChatworkReportNotifier,
    TeamsReportNotifier,
    WebhookSlackReportNotifier,
)


def test_send_report_posts_to_configured_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> None:
        calls.append({"url": url, "json": json, "timeout": timeout})

    monkeypatch.setattr("src.reports.dispatch.requests.post", fake_post)
    notifier = WebhookSlackReportNotifier("https://hooks.slack.com/services/xxx")

    notifier.send_report("日報テキスト")

    assert len(calls) == 1
    assert calls[0]["url"] == "https://hooks.slack.com/services/xxx"
    assert calls[0]["json"]["text"] == "日報テキスト"


def test_send_report_uses_env_var_when_url_not_explicitly_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "src.reports.dispatch.requests.post",
        lambda url, json, timeout: calls.append(url),
    )
    monkeypatch.setenv("SLACK_WEBHOOK_URL_REPORT", "https://hooks.slack.com/services/from-env")
    notifier = WebhookSlackReportNotifier()

    notifier.send_report("週報テキスト")

    assert calls == ["https://hooks.slack.com/services/from-env"]


def test_send_report_skips_when_no_webhook_url_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "src.reports.dispatch.requests.post",
        lambda url, json, timeout: calls.append(url),
    )
    monkeypatch.delenv("SLACK_WEBHOOK_URL_REPORT", raising=False)
    notifier = WebhookSlackReportNotifier()

    notifier.send_report("日報テキスト")

    assert calls == []


def test_teams_notifier_is_not_yet_implemented() -> None:
    with pytest.raises(NotImplementedError):
        TeamsReportNotifier().send_report("日報テキスト")


def test_chatwork_notifier_is_not_yet_implemented() -> None:
    with pytest.raises(NotImplementedError):
        ChatworkReportNotifier().send_report("日報テキスト")
