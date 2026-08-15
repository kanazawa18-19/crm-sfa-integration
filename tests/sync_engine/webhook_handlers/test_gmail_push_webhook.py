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
