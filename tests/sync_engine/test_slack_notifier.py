"""WebhookSlackNotifier（05_同期・競合制御「アラート通知」）の単体テスト。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.db_schema.base import Tool
from src.sync_engine.conflict_resolver import RejectedData
from src.sync_engine.slack_notifier import WebhookSlackNotifier

REJECTED = RejectedData(
    record_id="MSA-PJ-001",
    property_name="営業ステータス",
    adopted_value="商談中(B)",
    adopted_tool=Tool.NOTION,
    rejected_value="失注",
    rejected_tool=Tool.KINTONE,
    occurred_at=datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc),
)


def test_notify_conflict_posts_to_configured_webhook_url(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, json: dict[str, Any], timeout: int) -> None:
        calls.append({"url": url, "json": json, "timeout": timeout})

    monkeypatch.setattr("src.sync_engine.slack_notifier.requests.post", fake_post)
    notifier = WebhookSlackNotifier("https://hooks.slack.com/services/xxx")

    notifier.notify_conflict(REJECTED)

    assert len(calls) == 1
    assert calls[0]["url"] == "https://hooks.slack.com/services/xxx"
    text = calls[0]["json"]["text"]
    assert "MSA-PJ-001" in text
    assert "営業ステータス" in text
    assert "商談中(B)" in text
    assert "失注" in text
    assert "採用元: notion" in text
    assert "却下元: kintone" in text


def test_notify_conflict_uses_env_var_when_url_not_explicitly_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "src.sync_engine.slack_notifier.requests.post",
        lambda url, json, timeout: calls.append(url),
    )
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.com/services/from-env")
    notifier = WebhookSlackNotifier()

    notifier.notify_conflict(REJECTED)

    assert calls == ["https://hooks.slack.com/services/from-env"]


def test_notify_conflict_skips_when_no_webhook_url_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "src.sync_engine.slack_notifier.requests.post",
        lambda url, json, timeout: calls.append(url),
    )
    monkeypatch.delenv("SLACK_WEBHOOK_URL_ALERT", raising=False)
    notifier = WebhookSlackNotifier()

    notifier.notify_conflict(REJECTED)

    assert calls == []
