from __future__ import annotations

import json
from typing import Any

import pytest

from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.web_engagement_webhook import handler


class FakeContactNotionClient:
    """`ContactNotionClient` Protocolを満たすテスト用フェイク（実HTTP通信を行わない）。"""

    def __init__(self, pages: list[dict[str, Any]] | None = None) -> None:
        self.pages = pages or []
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.last_filter: dict[str, Any] | None = None
        self._next_id = 0

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.last_filter = filter
        return self.pages

    def create_page(self, properties: dict[str, Any]) -> str:
        self._next_id += 1
        page_id = f"new-page-{self._next_id}"
        self.created.append(properties)
        return page_id

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        self.updated.append((page_id, properties))


def _email_page(page_id: str, email: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {"メールアドレス": {"type": "email", "email": email}},
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    body = {
        "event_type": "hot_lead",
        "lead_id": "lead_123",
        "email": "yamada@example.com",
        "company": "株式会社サンプル",
        "last_name": "山田",
        "first_name": "太郎",
        "phone": "090-1111-2222",
        "score": 82,
        "assigned_rep_email": "sales@cnctor.jp",
        "portal_url": "https://web-engagement-tool.example.com/leads/lead_123",
    }
    body.update(overrides)
    return body


def _event(payload: dict[str, Any], *, secret: str | None = "correct-secret") -> dict[str, Any]:
    headers = {WEBHOOK_SECRET_HEADER: secret} if secret is not None else {}
    return {"body": json.dumps(payload), "headers": headers}


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_ENGAGEMENT_WEBHOOK_SECRET", "correct-secret")


# --- BLOCKER7: 共有シークレット検証 -----------------------------------------------------


def test_handler_returns_401_when_secret_mismatches() -> None:
    client = FakeContactNotionClient()

    response = handler(
        _event(_payload(), secret="wrong-secret"), context=None, notion_client=client
    )

    assert response["statusCode"] == 401
    assert client.created == []
    assert client.updated == []


def test_handler_returns_401_when_secret_header_missing() -> None:
    client = FakeContactNotionClient()

    response = handler(_event(_payload(), secret=None), context=None, notion_client=client)

    assert response["statusCode"] == 401


# --- BLOCKER5: 不正・欠損ペイロード時のエラーハンドリング -------------------------------


def test_handler_returns_400_for_malformed_json_body() -> None:
    event = {"body": "{not valid json", "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"}}

    response = handler(event, context=None, notion_client=FakeContactNotionClient())

    assert response["statusCode"] == 400


def test_handler_returns_400_when_email_missing() -> None:
    payload = _payload()
    del payload["email"]

    response = handler(_event(payload), context=None, notion_client=FakeContactNotionClient())

    assert response["statusCode"] == 400


def test_handler_returns_400_when_email_blank() -> None:
    response = handler(
        _event(_payload(email="   ")), context=None, notion_client=FakeContactNotionClient()
    )

    assert response["statusCode"] == 400


# --- 既存連絡先が見つかった場合: update_page ---------------------------------------------


def test_handler_updates_existing_contact_found_by_email() -> None:
    client = FakeContactNotionClient(pages=[_email_page("existing-page-1", "yamada@example.com")])

    response = handler(_event(_payload()), context=None, notion_client=client)

    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"page_id": "existing-page-1", "created": False}
    assert client.created == []
    assert len(client.updated) == 1
    page_id, properties = client.updated[0]
    assert page_id == "existing-page-1"
    assert properties["リードスコア"] == 82
    assert properties["Web接客ツールURL"] == (
        "https://web-engagement-tool.example.com/leads/lead_123"
    )
    assert "ホットリード化日時" in properties  # event_type == hot_lead
    # shirokuma-secレビューBLOCKER対応(2026-08-13): 携帯番号(sync_scope=ALL_TOOLS)は
    # 実Notion Webhook経由でZoho/kintoneへ無条件伝播してしまうため書き込まない。
    assert "携帯番号" not in properties


def test_handler_does_not_set_hot_lead_at_for_lead_upserted_event() -> None:
    client = FakeContactNotionClient(pages=[_email_page("existing-page-1", "yamada@example.com")])

    handler(_event(_payload(event_type="lead_upserted")), context=None, notion_client=client)

    _page_id, properties = client.updated[0]
    assert "ホットリード化日時" not in properties


def test_handler_skips_update_call_when_no_properties_to_update() -> None:
    client = FakeContactNotionClient(pages=[_email_page("existing-page-1", "yamada@example.com")])
    payload = _payload(event_type="lead_upserted", score=None, portal_url=None)

    response = handler(_event(payload), context=None, notion_client=client)

    assert response["statusCode"] == 200
    assert client.updated == []


# --- 見つからない場合: create_page -------------------------------------------------------


def test_handler_creates_new_contact_when_not_found() -> None:
    client = FakeContactNotionClient(pages=[])

    response = handler(_event(_payload()), context=None, notion_client=client)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["created"] is True
    assert len(client.created) == 1
    properties = client.created[0]
    assert properties["メールアドレス"] == "yamada@example.com"
    # WARN2: 「名前」には氏名のみを設定し、会社名は混入させない
    # （会社は取引先マスターリレーションで持つ設計だが、今回は自動解決しない）。
    assert properties["名前"] == "山田太郎"
    assert "取引先マスター" not in properties
    assert properties["リードスコア"] == 82
    assert properties["Web接客ツールURL"] == (
        "https://web-engagement-tool.example.com/leads/lead_123"
    )
    assert "ホットリード化日時" in properties
    # WARN3: 対応する受け皿プロパティが無いためassigned_rep_emailは書き込まない。
    assert "担当営業" not in properties
    # shirokuma-secレビューBLOCKER対応(2026-08-13): 携帯番号(sync_scope=ALL_TOOLS)は
    # 実Notion Webhook経由でZoho/kintoneへ無条件伝播してしまうため書き込まない。
    assert "携帯番号" not in properties


def test_handler_falls_back_to_email_for_name_when_no_name() -> None:
    client = FakeContactNotionClient(pages=[])
    payload = _payload(last_name=None, first_name=None)

    handler(_event(payload), context=None, notion_client=client)

    # 会社名(company)があっても「名前」には混入させないため、氏名が無ければ会社名も
    # 使わずemailへフォールバックする。
    assert client.created[0]["名前"] == "yamada@example.com"


# --- WARN7: 突合をNotion Query Database APIのfilterでNotion側に絞り込ませる -----------------


def test_handler_passes_email_filter_to_query_all_pages() -> None:
    client = FakeContactNotionClient(pages=[])

    handler(_event(_payload(email="yamada@example.com")), context=None, notion_client=client)

    assert client.last_filter == {
        "property": "メールアドレス",
        "email": {"equals": "yamada@example.com"},
    }


# --- WARN1: メールアドレス突合は大文字小文字を区別しない -----------------------------------


def test_handler_matches_existing_contact_case_insensitively() -> None:
    client = FakeContactNotionClient(
        pages=[_email_page("existing-page-1", "Yamada@Example.com")]
    )

    response = handler(
        _event(_payload(email="yamada@example.com")), context=None, notion_client=client
    )

    assert json.loads(response["body"]) == {"page_id": "existing-page-1", "created": False}
    assert client.created == []
