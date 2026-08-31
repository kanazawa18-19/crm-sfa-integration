"""Notion API Webhooksの署名検証と購読の検証ハンドシェイク（2026-08-31）。

**Notionはカスタムヘッダーを送れない**ので、他ツールで使っている `X-Webhook-Secret`
方式は成立しない。これを実装していなかったため、購読を作っても全イベントが401で
弾かれる状態だった（購読を作る直前に判明）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest

from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.webhook_handlers._common import (
    NOTION_SIGNATURE_HEADER,
    WEBHOOK_SECRET_HEADER,
    extract_notion_verification_token,
    verify_notion_webhook_signature,
)
from src.sync_engine.webhook_handlers.notion_webhook import handler_with_proxy

_SECRET = "secret_from_notion"
#: 実在するDBでないと整形後のペイロードがdb_keyへ解決できず400になる。
_DATABASE_ID = next(s.notion_database_id for s in ALL_SCHEMAS if s.key == "action")
_BOT_ID = "3b4d8ea8-d4f3-81ee-b550-0027586fe38e"


def _sign(body: str) -> str:
    return "sha256=" + hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()


class _FakeClient:
    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        return {
            "id": page_id,
            "parent": {"type": "database_id", "database_id": _DATABASE_ID},
            "last_edited_time": "2026-08-31T09:00:00.000Z",
            "last_edited_by": {"id": "human-user-id"},
            "properties": {},
        }


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_WEBHOOK_SECRET", _SECRET)
    monkeypatch.setenv("NOTION_SYNC_BOT_ID", _BOT_ID)


def test_signature_matches_body() -> None:
    body = '{"a":1}'

    assert verify_notion_webhook_signature({NOTION_SIGNATURE_HEADER: _sign(body)}, body)


def test_signature_of_a_different_body_is_rejected() -> None:
    """ボディを差し替えた再送を弾けること。"""
    assert not verify_notion_webhook_signature(
        {NOTION_SIGNATURE_HEADER: _sign('{"a":1}')}, '{"a":2}'
    )


def test_missing_signature_is_rejected() -> None:
    assert not verify_notion_webhook_signature({}, '{"a":1}')


def test_secret_not_configured_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """fail-closed。鍵が無ければ通さない。"""
    monkeypatch.delenv("NOTION_WEBHOOK_SECRET", raising=False)

    assert not verify_notion_webhook_signature(
        {NOTION_SIGNATURE_HEADER: _sign('{"a":1}')}, '{"a":1}'
    )


def test_verification_token_is_extracted() -> None:
    assert extract_notion_verification_token({"verification_token": "tok"}) == "tok"
    assert extract_notion_verification_token({}) is None


def test_verification_request_is_accepted_while_the_secret_is_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """購読作成時の検証リクエストには署名が無い（鍵をまだこちらが知らないため）。"""
    monkeypatch.delenv("NOTION_WEBHOOK_SECRET", raising=False)
    body = json.dumps({"verification_token": "tok_abcdefghijklmnop"})

    result = handler_with_proxy(
        {"headers": {}, "body": body}, None, notion_client=_FakeClient(), dispatcher=None
    )

    assert result["statusCode"] == 200
    assert json.loads(result["body"]) == {"received": "verification_token"}


def test_verification_token_is_not_written_to_the_log_in_full(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """トークンはそのまま HMAC鍵 になる値。ログ全文に残さない。"""
    monkeypatch.delenv("NOTION_WEBHOOK_SECRET", raising=False)
    token = "tok_abcdefghijklmnopqrstuvwxyz"

    with caplog.at_level("WARNING"):
        handler_with_proxy(
            {"headers": {}, "body": json.dumps({"verification_token": token})},
            None,
            notion_client=_FakeClient(),
            dispatcher=None,
        )

    assert all(token not in record.getMessage() for record in caplog.records)


def test_verification_request_is_rejected_once_the_secret_is_set() -> None:
    """鍵が設定済みなら検証リクエストはもう来ないはず。

    ここを恒久的に開けておくと、誰でも認証なしでPOSTできる口が残る
    （そこへ送った文字列がログに出る経路にもなっていた）。
    """
    result = handler_with_proxy(
        {"headers": {}, "body": json.dumps({"verification_token": "tok_abc"})},
        None,
        notion_client=_FakeClient(),
        dispatcher=None,
    )

    assert result["statusCode"] == 401


def test_signed_event_is_accepted() -> None:
    body = json.dumps({"entity": {"id": "page-1", "type": "page"}})

    result = handler_with_proxy(
        {"headers": {NOTION_SIGNATURE_HEADER: _sign(body)}, "body": body},
        None,
        notion_client=_FakeClient(),
        dispatcher=None,
    )

    assert result["statusCode"] == 200


def test_unsigned_event_is_rejected() -> None:
    body = json.dumps({"entity": {"id": "page-1", "type": "page"}})

    result = handler_with_proxy(
        {"headers": {}, "body": body}, None, notion_client=_FakeClient(), dispatcher=None
    )

    assert result["statusCode"] == 401


def test_legacy_shared_secret_is_rejected() -> None:
    """従来の`X-Webhook-Secret`方式は受け付けない（2026-08-31に廃止）。

    署名方式では鍵そのものはネットワークに流れないのに、同じ値をヘッダーで送れるように
    しておくと、鍵をBearerトークンとして扱うのと同じになる。どこかで漏れれば、
    以後その相手は正しい署名をいくらでも作れる。
    """
    body = json.dumps({"entity": {"id": "page-1", "type": "page"}})

    result = handler_with_proxy(
        {"headers": {WEBHOOK_SECRET_HEADER: _SECRET}, "body": body},
        None,
        notion_client=_FakeClient(),
        dispatcher=None,
    )

    assert result["statusCode"] == 401
