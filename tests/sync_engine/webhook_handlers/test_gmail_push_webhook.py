from __future__ import annotations

import base64
import json

import pytest

from src.gmail_sync import db
from src.sync_engine.webhook_handlers.gmail_push_webhook import handler


class FakeContactClient:
    pass


def _pubsub_body(email_address: str | None = "rep@cnctor.jp", history_id: str = "5000") -> str:
    inner: dict = {}
    if email_address is not None:
        inner["emailAddress"] = email_address
    inner["historyId"] = history_id
    data = base64.b64encode(json.dumps(inner).encode("utf-8")).decode("ascii")
    return json.dumps(
        {
            "message": {"data": data, "messageId": "1", "publishTime": "2026-08-16T00:00:00Z"},
            "subscription": "projects/test/subscriptions/gmail-push",
        }
    )


def _event(body: str, *, token: str | None = "correct-token") -> dict:
    query_params = {"token": token} if token is not None else {}
    return {"headers": {}, "body": body, "query_params": query_params}


@pytest.fixture(autouse=True)
def _token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_PUBSUB_VERIFICATION_TOKEN", "correct-token")
    monkeypatch.setenv("INTERNAL_EMAIL_DOMAINS", "cnctor.jp")


class FakeLockConnection:
    """`try_acquire_push_sync_lock()`が返す接続の代役(閉じられたかだけ記録する)。"""

    def __init__(self) -> None:
        self.released_for: list[str] = []


@pytest.fixture(autouse=True)
def _push_sync_lock(monkeypatch: pytest.MonkeyPatch) -> FakeLockConnection:
    """既定では「ロックが取れた」状態にする(実DBに繋がない)。取れなかった場合は各テストで上書き。"""
    fake = FakeLockConnection()
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.try_acquire_push_sync_lock",
        lambda rep_email: fake,
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.release_push_sync_lock",
        lambda conn, rep_email: conn.released_for.append(rep_email),
    )
    return fake


def _connection(rep_email: str = "rep@cnctor.jp") -> db.RepGmailConnection:
    return db.RepGmailConnection(
        rep_email=rep_email,
        refresh_token_enc="enc",
        last_synced_at=None,
        history_id="1000",
        watch_expiration=None,
    )


def test_handler_returns_401_when_token_mismatches() -> None:
    response = handler(_event(_pubsub_body(), token="wrong-token"), context=None)
    assert response["statusCode"] == 401


def test_handler_returns_200_when_payload_is_invalid_json() -> None:
    response = handler(_event("{not valid json"), context=None)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["processed"] is False


def test_handler_returns_200_when_email_address_missing() -> None:
    response = handler(_event(_pubsub_body(email_address=None)), context=None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["processed"] is False
    assert body["reason"] == "missing_email_address"


def test_handler_returns_200_when_rep_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: None,
    )

    response = handler(_event(_pubsub_body()), context=None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["processed"] is False
    assert body["reason"] == "unknown_rep"


def test_handler_calls_sync_rep_incremental_when_rep_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: _connection(rep_email),
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.decrypt_token",
        lambda enc: "refresh-token",
    )
    calls: list[tuple] = []
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda rep_email, refresh_token, contact_client, *, internal_domains: calls.append(
            (rep_email, refresh_token, internal_domains)
        )
        or 2,
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["processed"] is True
    assert body["logged_count"] == 2
    assert calls == [("rep@cnctor.jp", "refresh-token", frozenset({"cnctor.jp"}))]


def test_handler_lowercases_email_address_before_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    # shirokuma-secレビューWARN対応(2026-08-16): Pub/Subペイロードのemailアドレスの
    # 大文字小文字ゆれを比較前に吸収する(`_extract_addresses()`等、他の箇所との一貫性)。
    lookups: list[str] = []
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: lookups.append(rep_email) or _connection("rep@cnctor.jp"),
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.decrypt_token",
        lambda enc: "refresh-token",
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda rep_email, refresh_token, contact_client, *, internal_domains: 0,
    )

    response = handler(
        _event(_pubsub_body(email_address="Rep@CNCTOR.JP")), context=None, contact_client=FakeContactClient()
    )

    assert response["statusCode"] == 200
    assert lookups == ["rep@cnctor.jp"]


def test_handler_returns_200_when_sync_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: _connection(rep_email),
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.decrypt_token",
        lambda enc: "refresh-token",
    )

    def fail_sync(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental", fail_sync
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["processed"] is False
    assert body["reason"] == "error"


def test_handler_acknowledges_without_sync_when_same_mailbox_is_already_syncing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停止からの復旧直後に溜まった通知が一斉に届いても、同じメールボックスの同期は1本だけ。
    ロックが取れなかった分は同期を呼ばず、即200で返してPub/Subにackさせる(再送の嵐を防ぐ)。"""
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: _connection(rep_email),
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.try_acquire_push_sync_lock",
        lambda rep_email: None,
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda *args, **kwargs: calls.append("called") or 0,
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body == {"processed": False, "reason": "already_syncing"}
    assert calls == []


def test_handler_releases_push_sync_lock_even_when_sync_raises(
    monkeypatch: pytest.MonkeyPatch, _push_sync_lock: FakeLockConnection
) -> None:
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: _connection(rep_email),
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.decrypt_token",
        lambda enc: "refresh-token",
    )

    def fail_sync(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental", fail_sync
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert json.loads(response["body"])["reason"] == "error"
    assert _push_sync_lock.released_for == ["rep@cnctor.jp"]


def test_handler_releases_push_sync_lock_after_successful_sync(
    monkeypatch: pytest.MonkeyPatch, _push_sync_lock: FakeLockConnection
) -> None:
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: _connection(rep_email),
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.decrypt_token",
        lambda enc: "refresh-token",
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda *args, **kwargs: 1,
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert json.loads(response["body"]) == {"processed": True, "logged_count": 1}
    assert _push_sync_lock.released_for == ["rep@cnctor.jp"]
