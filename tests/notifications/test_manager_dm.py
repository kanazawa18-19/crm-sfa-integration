"""`src.notifications.manager_dm`（`User.isManager = true`全員へのSlack DM共通ヘルパー）の
単体テスト。`tests/incident_detection/test_notify.py`の`notify_managers_immediate`向けテストと
同じ観点（SLACK_BOT_TOKEN未設定/manager0人/DB失敗のいずれも静かにスキップ、対象者ごとに
独立してtry/exceptする）を検証する。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.notifications import manager_dm


class _FakeSlackResponse:
    """`requests.post(...).json()`のダミー戻り値。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def test_notify_managers_skips_when_slack_bot_token_not_configured(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(
        manager_dm,
        "find_manager_emails",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )

    with caplog.at_level("WARNING"):
        manager_dm.notify_managers("text", log_context="test")

    # shirokuma-secレビュー対応(2026-08-25): 未設定時も痕跡がログに残ること。
    assert any("SLACK_BOT_TOKEN is not configured" in r.getMessage() for r in caplog.records)


def test_notify_managers_skips_when_no_managers_found(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(manager_dm, "find_manager_emails", lambda: [])
    resolve_calls: list[str] = []
    monkeypatch.setattr(manager_dm, "_resolve_dm_channel", lambda email, **kwargs: resolve_calls.append(email))

    with caplog.at_level("WARNING"):
        manager_dm.notify_managers("text", log_context="test")

    assert resolve_calls == []
    # shirokuma-secレビュー対応(2026-08-25): 0人時も痕跡がログに残ること。
    assert any("no managers found" in r.getMessage() for r in caplog.records)

    assert resolve_calls == []


def test_notify_managers_skips_silently_when_find_manager_emails_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    def fail_find_manager_emails() -> list[str]:
        raise RuntimeError("db connection error")

    monkeypatch.setattr(manager_dm, "find_manager_emails", fail_find_manager_emails)

    # 例外を送出せず静かに失敗を吸収する（メイン処理を止めない設計）。
    manager_dm.notify_managers("text", log_context="test")


def test_notify_managers_sends_dm_to_each_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        manager_dm, "find_manager_emails", lambda: ["kanazawa@cnctor.jp", "hiramoto@cnctor.jp"]
    )
    resolved_channels = {"kanazawa@cnctor.jp": ("C-KANAZAWA", "U1"), "hiramoto@cnctor.jp": ("C-HIRAMOTO", "U2")}
    monkeypatch.setattr(manager_dm, "_resolve_dm_channel", lambda email, **kwargs: resolved_channels[email])

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        manager_dm.requests,
        "post",
        lambda url, headers, json, timeout: calls.append({"url": url, "json": json})
        or _FakeSlackResponse({"ok": True}),
    )

    manager_dm.notify_managers("[新規レコード自動作成]\nDB: client_master", log_context="test")

    assert len(calls) == 2
    channels_notified = {call["json"]["channel"] for call in calls}
    assert channels_notified == {"C-KANAZAWA", "C-HIRAMOTO"}
    for call in calls:
        assert "client_master" in call["json"]["text"]


def test_notify_managers_continues_to_next_manager_when_one_dm_channel_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        manager_dm, "find_manager_emails", lambda: ["kanazawa@cnctor.jp", "hiramoto@cnctor.jp"]
    )

    def fake_resolve(email: str, **kwargs: Any) -> tuple[str, str] | None:
        return None if email == "kanazawa@cnctor.jp" else ("C-HIRAMOTO", "U2")

    monkeypatch.setattr(manager_dm, "_resolve_dm_channel", fake_resolve)

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        manager_dm.requests,
        "post",
        lambda url, headers, json, timeout: calls.append({"url": url, "json": json})
        or _FakeSlackResponse({"ok": True}),
    )

    manager_dm.notify_managers("text", log_context="test")

    # 1人目の解決失敗があっても2人目へは送信される
    assert len(calls) == 1
    assert calls[0]["json"]["channel"] == "C-HIRAMOTO"


def test_notify_managers_continues_to_next_manager_when_one_post_message_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        manager_dm, "find_manager_emails", lambda: ["kanazawa@cnctor.jp", "hiramoto@cnctor.jp"]
    )
    resolved_channels = {"kanazawa@cnctor.jp": ("C-KANAZAWA", "U1"), "hiramoto@cnctor.jp": ("C-HIRAMOTO", "U2")}
    monkeypatch.setattr(manager_dm, "_resolve_dm_channel", lambda email, **kwargs: resolved_channels[email])

    calls: list[dict[str, Any]] = []

    def fake_post(url: str, headers: dict, json: dict, timeout: int) -> Any:
        if json["channel"] == "C-KANAZAWA":
            raise RuntimeError("network error")
        calls.append({"url": url, "json": json})
        return _FakeSlackResponse({"ok": True})

    monkeypatch.setattr(manager_dm.requests, "post", fake_post)

    # 例外を送出せず静かに失敗を吸収しつつ、他の対象者への送信は継続する。
    manager_dm.notify_managers("text", log_context="test")

    assert len(calls) == 1
    assert calls[0]["json"]["channel"] == "C-HIRAMOTO"


def test_notify_managers_continues_to_next_manager_when_one_chat_post_message_returns_not_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack Web APIはHTTP 200でもエラーをbody({"ok": false, ...})で返すため、そのケースも
    「1人の失敗が他へ伝播しない」ことを確認する。"""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        manager_dm, "find_manager_emails", lambda: ["kanazawa@cnctor.jp", "hiramoto@cnctor.jp"]
    )
    resolved_channels = {"kanazawa@cnctor.jp": ("C-KANAZAWA", "U1"), "hiramoto@cnctor.jp": ("C-HIRAMOTO", "U2")}
    monkeypatch.setattr(manager_dm, "_resolve_dm_channel", lambda email, **kwargs: resolved_channels[email])

    calls: list[dict[str, Any]] = []

    def fake_post(url: str, headers: dict, json: dict, timeout: int) -> Any:
        calls.append({"url": url, "json": json})
        if json["channel"] == "C-KANAZAWA":
            return _FakeSlackResponse({"ok": False, "error": "channel_not_found"})
        return _FakeSlackResponse({"ok": True})

    monkeypatch.setattr(manager_dm.requests, "post", fake_post)

    manager_dm.notify_managers("text", log_context="test")

    assert len(calls) == 2  # 両方に送信は試みるが、1人目のエラーは握りつぶす


def test_send_dm_raises_when_dm_channel_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_dm, "_resolve_dm_channel", lambda email, **kwargs: None)

    with pytest.raises(RuntimeError, match="Slackユーザー解決に失敗"):
        manager_dm.send_dm("kanazawa@cnctor.jp", "text")


def test_send_dm_raises_when_chat_post_message_returns_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manager_dm, "_resolve_dm_channel", lambda email, **kwargs: ("C-KANAZAWA", "U1"))
    monkeypatch.setattr(
        manager_dm.requests,
        "post",
        lambda url, headers, json, timeout: _FakeSlackResponse({"ok": False, "error": "not_in_channel"}),
    )

    with pytest.raises(RuntimeError, match="chat.postMessage失敗"):
        manager_dm.send_dm("kanazawa@cnctor.jp", "text")


def test_send_dm_uses_short_timeout_by_default_for_both_slack_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """shirokuma-secレビュー対応(2026-08-25): 各Slack API呼び出し自体のtimeoutが
    `_meeting_sync/slack_approval.py`の10秒より短い`_DM_API_CALL_TIMEOUT_SECONDS`(3秒)に
    なっていること。"""
    resolve_timeouts: list[float] = []

    def fake_resolve(email: str, *, timeout: float) -> tuple[str, str]:
        resolve_timeouts.append(timeout)
        return ("C-KANAZAWA", "U1")

    monkeypatch.setattr(manager_dm, "_resolve_dm_channel", fake_resolve)

    post_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        manager_dm.requests,
        "post",
        lambda url, headers, json, timeout: post_calls.append({"timeout": timeout})
        or _FakeSlackResponse({"ok": True}),
    )

    manager_dm.send_dm("kanazawa@cnctor.jp", "text")

    assert resolve_timeouts == [manager_dm._DM_API_CALL_TIMEOUT_SECONDS]
    assert post_calls[0]["timeout"] == manager_dm._DM_API_CALL_TIMEOUT_SECONDS
    assert manager_dm._DM_API_CALL_TIMEOUT_SECONDS < 10


# --- 全体タイムアウト予算（2026-08-25、shirokuma-secレビュー【最重要】対応） -----------------


def test_notify_managers_stops_notifying_remaining_managers_once_time_budget_exceeded(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """`dispatcher.dispatch()`が同期的にDM送信を待つため、マネージャーが多数いても
    Webhookレスポンスを長時間ブロックしないよう、予算超過後は残りをスキップすること。"""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(
        manager_dm,
        "find_manager_emails",
        lambda: ["m1@cnctor.jp", "m2@cnctor.jp", "m3@cnctor.jp"],
    )

    sent_to: list[str] = []

    def fake_send_dm(manager_email: str, text: str) -> None:
        sent_to.append(manager_email)

    monkeypatch.setattr(manager_dm, "send_dm", fake_send_dm)

    # 1人目の送信直後に予算を使い切ったことにする(time.monotonic()の戻り値を差し替える)。
    call_count = {"n": 0}
    real_monotonic = manager_dm.time.monotonic

    def fake_monotonic() -> float:
        call_count["n"] += 1
        base = real_monotonic()
        # 1回目(deadline計算)・2回目(1人目のループ開始チェック)はそのまま、
        # 3回目(2人目のループ開始チェック)以降で予算を使い切ったことにする。
        if call_count["n"] <= 2:
            return base
        return base + manager_dm._NOTIFY_MANAGERS_TIME_BUDGET_SECONDS + 1

    monkeypatch.setattr(manager_dm.time, "monotonic", fake_monotonic)

    with caplog.at_level("WARNING"):
        manager_dm.notify_managers("text", log_context="test")

    assert sent_to == ["m1@cnctor.jp"]
    assert any("exceeded the" in r.getMessage() and "time budget" in r.getMessage() for r in caplog.records)
