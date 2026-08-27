"""見積書承認リクエスト結果のSlack DM通知(`approval_notify.notify_quote_approval_result`)の
単体テスト。通知失敗時も例外を送出しない契約(cronの他の承認リクエスト処理を止めないため)を
重点的に検証する。
"""

from __future__ import annotations

import pytest

from src.document_generation import approval_notify
from src.document_generation.approval_notify import notify_quote_approval_result


def test_notify_does_nothing_when_slack_bot_token_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)

    notify_quote_approval_result(
        requested_by_email="rep@example.com",
        project_name="テスト案件",
        approver_emails=["approver@example.com"],
        status="approved",
    )  # 例外を送出しないことを確認


def test_notify_does_not_raise_when_dm_channel_resolution_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(approval_notify, "_resolve_dm_channel", lambda rep_email: None)

    notify_quote_approval_result(
        requested_by_email="rep@example.com",
        project_name="テスト案件",
        approver_emails=["approver@example.com"],
        status="approved",
    )


def test_notify_does_not_raise_when_dm_channel_resolution_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_resolve_dm_channel`自体が例外を送出しても(Slack API疎通不可等)、通知は
    ベストエフォートであるべきなので例外を伝播させない(obasan-qualityレビューWARN対応)。"""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    def _raise_resolve(rep_email: str) -> tuple[str, str] | None:
        raise RuntimeError("slack api boom")

    monkeypatch.setattr(approval_notify, "_resolve_dm_channel", _raise_resolve)

    notify_quote_approval_result(
        requested_by_email="rep@example.com",
        project_name="テスト案件",
        approver_emails=["approver@example.com"],
        status="approved",
    )  # 例外を送出しないことを確認


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def test_notify_posts_message_to_resolved_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(approval_notify, "_resolve_dm_channel", lambda rep_email: ("C123", "U123"))

    posted: list[dict] = []
    monkeypatch.setattr(
        approval_notify.requests,
        "post",
        lambda url, headers, json, timeout: posted.append({"url": url, "json": json})
        or _FakeResponse({"ok": True}),
    )

    notify_quote_approval_result(
        requested_by_email="rep@example.com",
        project_name="テスト案件",
        approver_emails=["approver@example.com"],
        status="approved",
    )

    assert posted[0]["json"]["channel"] == "C123"
    assert "テスト案件" in posted[0]["json"]["text"]


def test_notify_lists_multiple_approver_emails_as_bullet_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """複数承認者(2026-08-27対応)を渡した場合、Slack文面の承認者一覧は`、`区切りの1行では
    なく箇条書き(改行＋`・`)になる(人数が増えると読みにくいため、obasan-qualityレビュー
    WARN対応)。"""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(approval_notify, "_resolve_dm_channel", lambda rep_email: ("C123", "U123"))

    posted: list[dict] = []
    monkeypatch.setattr(
        approval_notify.requests,
        "post",
        lambda url, headers, json, timeout: posted.append({"url": url, "json": json})
        or _FakeResponse({"ok": True}),
    )

    notify_quote_approval_result(
        requested_by_email="rep@example.com",
        project_name="テスト案件",
        approver_emails=["a@example.com", "b@example.com"],
        status="approved",
    )

    text = posted[0]["json"]["text"]
    assert "承認者:\n・a@example.com\n・b@example.com" in text


def test_notify_declined_includes_declined_reviewer_when_extractable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`approval_state`の`reviewerResponses`から却下者を特定できる場合、Slack文面に
    「却下した承認者:」として明示する(WARN対応)。"""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(approval_notify, "_resolve_dm_channel", lambda rep_email: ("C123", "U123"))

    posted: list[dict] = []
    monkeypatch.setattr(
        approval_notify.requests,
        "post",
        lambda url, headers, json, timeout: posted.append({"url": url, "json": json})
        or _FakeResponse({"ok": True}),
    )

    notify_quote_approval_result(
        requested_by_email="rep@example.com",
        project_name="テスト案件",
        approver_emails=["a@example.com", "b@example.com"],
        status="declined",
        approval_state={
            "reviewerResponses": [
                {"reviewer": "a@example.com", "response": "APPROVED"},
                {"reviewer": "b@example.com", "response": "DECLINED"},
            ]
        },
    )

    text = posted[0]["json"]["text"]
    assert "却下した承認者: b@example.com" in text


def test_notify_declined_falls_back_to_listing_all_approvers_when_reviewer_responses_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reviewerResponses`が無い(未検証のとおり実レスポンスに含まれない)場合、例外にならず
    従来どおり承認者全員の列挙のみになる。"""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(approval_notify, "_resolve_dm_channel", lambda rep_email: ("C123", "U123"))

    posted: list[dict] = []
    monkeypatch.setattr(
        approval_notify.requests,
        "post",
        lambda url, headers, json, timeout: posted.append({"url": url, "json": json})
        or _FakeResponse({"ok": True}),
    )

    notify_quote_approval_result(
        requested_by_email="rep@example.com",
        project_name="テスト案件",
        approver_emails=["a@example.com", "b@example.com"],
        status="declined",
        approval_state={"kind": "drive#approval", "status": "DECLINED"},
    )

    text = posted[0]["json"]["text"]
    assert "却下した承認者" not in text
    assert "承認者:\n・a@example.com\n・b@example.com" in text


def test_notify_declined_falls_back_when_reviewer_responses_has_unexpected_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`reviewerResponses`があっても要素の形が想定と違う(未検証の実レスポンス差異)場合も
    例外を送出せず全員列挙にフォールバックする。"""
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(approval_notify, "_resolve_dm_channel", lambda rep_email: ("C123", "U123"))
    monkeypatch.setattr(
        approval_notify.requests,
        "post",
        lambda url, headers, json, timeout: _FakeResponse({"ok": True}),
    )

    # 例外を送出しないことを確認するのが主目的。
    approval_notify._extract_declined_reviewers({"reviewerResponses": "not-a-list"})
    approval_notify._extract_declined_reviewers({"reviewerResponses": [None, 123, "str"]})
    approval_notify._extract_declined_reviewers({"reviewerResponses": [{"response": "DECLINED"}]})


def test_notify_does_not_raise_when_chat_post_message_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(approval_notify, "_resolve_dm_channel", lambda rep_email: ("C123", "U123"))
    monkeypatch.setattr(
        approval_notify.requests,
        "post",
        lambda url, headers, json, timeout: _FakeResponse({"ok": False, "error": "channel_not_found"}),
    )

    notify_quote_approval_result(
        requested_by_email="rep@example.com",
        project_name="テスト案件",
        approver_emails=["approver@example.com"],
        status="declined",
    )
