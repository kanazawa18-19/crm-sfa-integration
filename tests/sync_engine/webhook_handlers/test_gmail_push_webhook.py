from __future__ import annotations

import base64
import json
import time

import pytest

from src.gmail_sync import db
from src.sync_engine.clients._http import (
    _MAX_RATE_LIMIT_BACKOFF_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    INTERACTIVE_MAX_RATE_LIMIT_RETRIES,
)
from src.sync_engine.webhook_handlers import gmail_push_webhook
from src.sync_engine.webhook_handlers.gmail_push_webhook import PUSH_SYNC_TIME_BUDGET_SECONDS, handler


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
    results = iter([2, 0])
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda rep_email, refresh_token, contact_client, *, internal_domains, deadline: calls.append(
            (rep_email, refresh_token, internal_domains)
        )
        or next(results),
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["processed"] is True
    assert body["logged_count"] == 2
    # 1周目で記録があったので、その間に届いた分を拾う2周目が走る(2周目は0件)。
    assert calls == [("rep@cnctor.jp", "refresh-token", frozenset({"cnctor.jp"}))] * 2


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
        lambda rep_email, refresh_token, contact_client, *, internal_domains, deadline: 0,
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
    results = iter([1, 0])
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda *args, **kwargs: next(results),
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert json.loads(response["body"]) == {"processed": True, "logged_count": 1}
    assert _push_sync_lock.released_for == ["rep@cnctor.jp"]


def test_handler_passes_a_deadline_based_on_the_push_time_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Push経路(Vercel関数、300秒で強制終了)では全体に時間予算を掛ける(2026-09-21の
    本番障害対応)。429リトライの絞りは`sync_rep_incremental()`側が期限とセットで行う。"""
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: _connection(rep_email),
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.decrypt_token",
        lambda enc: "refresh-token",
    )
    seen: dict = {}

    def fake_sync(rep_email, refresh_token, contact_client, *, internal_domains, deadline):
        seen["deadline"] = deadline
        return 0

    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental", fake_sync
    )

    before = time.monotonic()
    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())
    after = time.monotonic()

    assert response["statusCode"] == 200
    assert before + PUSH_SYNC_TIME_BUDGET_SECONDS <= seen["deadline"] <= after + PUSH_SYNC_TIME_BUDGET_SECONDS


def test_push_time_budget_leaves_headroom_for_one_worst_case_api_call() -> None:
    """期限の判定はメッセージ1件ごとなので、期限直前に始めた外部API呼び出し1回が最悪まで
    長引いても、Vercel関数の上限300秒に収まること(定数から検算する。obasan-qualityレビュー
    WARN対応: 根拠の無い数字との比較にしない)。"""
    vercel_max_duration = 300.0
    worst_single_call = DEFAULT_TIMEOUT_SECONDS * (INTERACTIVE_MAX_RATE_LIMIT_RETRIES + 1) + (
        _MAX_RATE_LIMIT_BACKOFF_SECONDS * INTERACTIVE_MAX_RATE_LIMIT_RETRIES
    )
    assert 0 < PUSH_SYNC_TIME_BUDGET_SECONDS
    assert PUSH_SYNC_TIME_BUDGET_SECONDS + worst_single_call <= vercel_max_duration


def test_default_contact_client_bounds_notion_rate_limit_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Notion側も同じくリクエスト/レスポンス型向けの小さいリトライ回数を明示する
    (`_http.py`の指針。既定の30回だと最悪15分待ちで関数ごとタイムアウトする)。"""
    captured: dict = {}

    class FakeSchema:
        notion_database_id = "db-123"

    class FakeNotionClient:
        def __init__(self, db_key, database_id, **kwargs):
            captured["db_key"] = db_key
            captured["database_id"] = database_id
            captured.update(kwargs)

    monkeypatch.setattr(gmail_push_webhook, "get_schema", lambda key: FakeSchema())
    monkeypatch.setattr(gmail_push_webhook, "HttpNotionClient", FakeNotionClient)

    gmail_push_webhook._default_contact_client()

    assert captured["db_key"] == "contact"
    assert captured["database_id"] == "db-123"
    assert captured["max_rate_limit_retries"] == INTERACTIVE_MAX_RATE_LIMIT_RETRIES


def _rep_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.db.find_connection_by_email",
        lambda rep_email: _connection(rep_email),
    )
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.decrypt_token",
        lambda enc: "refresh-token",
    )


def test_handler_does_not_run_a_second_pass_when_the_first_pass_logged_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1周目が0件(=すぐ終わった)なら、その間に通知を取りこぼした可能性は無いので1周で終える。"""
    _rep_found(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda *args, **kwargs: calls.append(1) or 0,
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert json.loads(response["body"]) == {"processed": True, "logged_count": 0}
    assert len(calls) == 1


def test_handler_skips_the_second_pass_when_the_time_budget_is_already_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1周目で記録があっても、期限を過ぎていれば2周目は始めない(300秒内に必ず返す)。"""
    _rep_found(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda *args, **kwargs: calls.append(1) or 5,
    )
    ticks = iter([0.0, PUSH_SYNC_TIME_BUDGET_SECONDS + 1.0])
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.time.monotonic", lambda: next(ticks)
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert json.loads(response["body"]) == {"processed": True, "logged_count": 5}
    assert len(calls) == 1


def test_handler_runs_a_second_pass_with_the_same_deadline_when_the_first_pass_logged_mail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """2周目は1周目と同じ期限を使う(周回で時間予算が伸びない)。"""
    _rep_found(monkeypatch)
    deadlines: list[float] = []
    results = iter([3, 1])
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.gmail_push_webhook.sync.sync_rep_incremental",
        lambda *args, deadline, **kwargs: deadlines.append(deadline) or next(results),
    )

    response = handler(_event(_pubsub_body()), context=None, contact_client=FakeContactClient())

    assert json.loads(response["body"]) == {"processed": True, "logged_count": 4}
    assert len(deadlines) == 2
    assert deadlines[0] == deadlines[1]
