from __future__ import annotations

import pytest

from src.api.token_encryption_healthcheck import (
    check_token_encryption_key,
    run_token_encryption_healthcheck,
)

_TEST_KEY = "0" * 64  # 32byte hex


def test_check_returns_ok_when_key_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", _TEST_KEY)

    result = check_token_encryption_key()

    assert result == {"ok": True, "error": None}


def test_check_returns_error_when_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)

    result = check_token_encryption_key()

    assert result["ok"] is False
    assert "TOKEN_ENCRYPTION_KEY" in result["error"]


def test_check_returns_error_when_key_wrong_length(monkeypatch: pytest.MonkeyPatch) -> None:
    # 2026-08-16〜08-18に実際に発生した障害(不正な長さの値が設定されていた)の再現。
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not-a-valid-hex-key")

    result = check_token_encryption_key()

    assert result["ok"] is False


def test_run_healthcheck_posts_slack_alert_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.test/alert")

    posted = {}

    def fake_post(url: str, json: dict, timeout: float) -> None:
        posted["url"] = url
        posted["text"] = json["text"]

    monkeypatch.setattr("src.api.token_encryption_healthcheck.requests.post", fake_post)

    result = run_token_encryption_healthcheck()

    assert result["ok"] is False
    assert posted["url"] == "https://hooks.slack.test/alert"
    assert "TOKEN_ENCRYPTION_KEY" in posted["text"]


def test_run_healthcheck_does_not_post_slack_alert_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", _TEST_KEY)
    monkeypatch.setenv("SLACK_WEBHOOK_URL_ALERT", "https://hooks.slack.test/alert")

    def fail_post(*args, **kwargs):
        raise AssertionError("should not post to slack when healthcheck succeeds")

    monkeypatch.setattr("src.api.token_encryption_healthcheck.requests.post", fail_post)

    result = run_token_encryption_healthcheck()

    assert result == {"ok": True, "error": None}


def test_run_healthcheck_skips_slack_post_when_webhook_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("SLACK_WEBHOOK_URL_ALERT", raising=False)

    def fail_post(*args, **kwargs):
        raise AssertionError("should not attempt to post when webhook url is unset")

    monkeypatch.setattr("src.api.token_encryption_healthcheck.requests.post", fail_post)

    result = run_token_encryption_healthcheck()

    assert result["ok"] is False
