from __future__ import annotations

import json
from typing import Any

import pytest

from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.lead_inquiry_webhook import handler


class FakeNotionPageClient:
    """`NotionPageClient` Protocolを満たすテスト用フェイク（実HTTP通信を行わない）。"""

    def __init__(
        self,
        *,
        pages: list[dict[str, Any]] | None = None,
        flat_pages: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.pages = pages or []
        self.flat_pages = flat_pages or {}
        self.created: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []
        self.last_filter: dict[str, Any] | None = None
        self._next_id = 0

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        self.last_filter = filter
        return self.pages

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        return self.flat_pages.get(page_id)

    def create_page(self, properties: dict[str, Any]) -> str:
        self._next_id += 1
        page_id = f"new-page-{self._next_id}"
        self.created.append(properties)
        return page_id

    def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        self.updated.append((page_id, properties))


def _email_page(page_id: str, email: str) -> dict[str, Any]:
    return {"id": page_id, "properties": {"メールアドレス": {"type": "email", "email": email}}}


def _title_page(page_id: str, property_name: str, value: str) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {property_name: {"type": "title", "title": [{"plain_text": value}]}},
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    body = {
        "company": "株式会社サンプル温泉",
        "name": "山田太郎",
        "email": "yamada@example.com",
        "phone": "090-1111-2222",
    }
    body.update(overrides)
    return body


def _event(payload: dict[str, Any], *, secret: str | None = "correct-secret") -> dict[str, Any]:
    headers = {WEBHOOK_SECRET_HEADER: secret} if secret is not None else {}
    return {"body": json.dumps(payload), "headers": headers}


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEAD_RESEARCHER_WEBHOOK_SECRET", "correct-secret")


def _handle(
    payload: dict[str, Any],
    *,
    contact_client: FakeNotionPageClient | None = None,
    client_master_client: FakeNotionPageClient | None = None,
    secret: str | None = "correct-secret",
) -> dict[str, Any]:
    return handler(
        _event(payload, secret=secret),
        context=None,
        contact_client=contact_client or FakeNotionPageClient(),
        client_master_client=client_master_client or FakeNotionPageClient(),
    )


# --- 認証 ---------------------------------------------------------------------------------


def test_handler_returns_401_when_secret_mismatches() -> None:
    contact = FakeNotionPageClient()

    response = _handle(_payload(), contact_client=contact, secret="wrong-secret")

    assert response["statusCode"] == 401
    assert contact.created == []


def test_handler_returns_401_when_secret_header_missing() -> None:
    response = _handle(_payload(), secret=None)

    assert response["statusCode"] == 401


# --- 不正・欠損ペイロード ------------------------------------------------------------------


def test_handler_returns_400_for_malformed_json_body() -> None:
    event = {"body": "{not valid json", "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"}}

    response = handler(
        event, context=None, contact_client=FakeNotionPageClient(), client_master_client=FakeNotionPageClient()
    )

    assert response["statusCode"] == 400


def test_handler_returns_400_when_email_and_name_both_missing() -> None:
    payload = _payload(email="", name="")

    response = _handle(payload)

    assert response["statusCode"] == 400


@pytest.mark.parametrize("raw_body", ["null", "[1, 2, 3]", "42", "true", '"just a string"'])
def test_handler_returns_400_for_syntactically_valid_but_non_dict_json_body(raw_body: str) -> None:
    """構文的には正しいJSONでも辞書でない場合、payload.get()のAttributeErrorが未捕捉の
    まま漏れないことを確認する（zoho_webhook.pyのBLOCKER2対応と同種のガード）。"""
    event = {"body": raw_body, "headers": {WEBHOOK_SECRET_HEADER: "correct-secret"}}

    response = handler(
        event, context=None, contact_client=FakeNotionPageClient(), client_master_client=FakeNotionPageClient()
    )

    assert response["statusCode"] == 400


# --- 新規作成 ------------------------------------------------------------------------------


def test_handler_creates_new_contact_when_not_found() -> None:
    contact = FakeNotionPageClient(pages=[])

    response = _handle(_payload(), contact_client=contact)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body == {"page_id": "new-page-1", "created": True, "matched_client_master": False}
    assert contact.created == [{"名前": "山田太郎", "メールアドレス": "yamada@example.com", "携帯番号": "090-1111-2222"}]


def test_handler_falls_back_to_email_for_name_when_name_missing() -> None:
    contact = FakeNotionPageClient(pages=[])

    _handle(_payload(name="", phone=""), contact_client=contact)

    assert contact.created[0]["名前"] == "yamada@example.com"


def test_handler_links_client_master_on_exact_company_match() -> None:
    contact = FakeNotionPageClient(pages=[])
    client_master = FakeNotionPageClient(
        pages=[_title_page("cm-1", "取引先名", "株式会社サンプル温泉")]
    )

    response = _handle(_payload(), contact_client=contact, client_master_client=client_master)

    assert json.loads(response["body"])["matched_client_master"] is True
    assert contact.created[0]["取引先マスター"] == ["cm-1"]


def test_handler_does_not_create_client_master_on_no_exact_match() -> None:
    contact = FakeNotionPageClient(pages=[])
    client_master = FakeNotionPageClient(pages=[_title_page("cm-1", "取引先名", "全く別の会社")])

    response = _handle(_payload(), contact_client=contact, client_master_client=client_master)

    assert json.loads(response["body"])["matched_client_master"] is False
    assert "取引先マスター" not in contact.created[0]
    assert client_master.created == []
    assert client_master.updated == []


# --- 既存連絡先の更新（空欄のみ埋める） -----------------------------------------------------


def test_handler_fills_empty_fields_on_existing_contact() -> None:
    contact = FakeNotionPageClient(
        pages=[_email_page("existing-1", "yamada@example.com")],
        flat_pages={"existing-1": {"メールアドレス": "yamada@example.com", "携帯番号": None, "取引先マスター": []}},
    )
    client_master = FakeNotionPageClient(
        pages=[_title_page("cm-1", "取引先名", "株式会社サンプル温泉")]
    )

    response = _handle(_payload(), contact_client=contact, client_master_client=client_master)

    assert json.loads(response["body"]) == {
        "page_id": "existing-1",
        "created": False,
        "matched_client_master": True,
    }
    assert contact.created == []
    assert len(contact.updated) == 1
    page_id, properties = contact.updated[0]
    assert page_id == "existing-1"
    # メールは既に埋まっているため上書き対象に含めない
    assert "メールアドレス" not in properties
    assert properties["携帯番号"] == "090-1111-2222"
    assert properties["取引先マスター"] == ["cm-1"]


def test_handler_does_not_overwrite_existing_non_empty_fields() -> None:
    contact = FakeNotionPageClient(
        pages=[_email_page("existing-1", "yamada@example.com")],
        flat_pages={
            "existing-1": {
                "メールアドレス": "yamada@example.com",
                "携帯番号": "080-9999-8888",
                "取引先マスター": ["already-linked"],
            }
        },
    )
    client_master = FakeNotionPageClient(
        pages=[_title_page("cm-1", "取引先名", "株式会社サンプル温泉")]
    )

    _handle(_payload(), contact_client=contact, client_master_client=client_master)

    assert contact.updated == []


def test_handler_skips_update_call_when_nothing_to_fill() -> None:
    contact = FakeNotionPageClient(
        pages=[_email_page("existing-1", "yamada@example.com")],
        flat_pages={
            "existing-1": {
                "メールアドレス": "yamada@example.com",
                "携帯番号": "080-9999-8888",
                "取引先マスター": ["already-linked"],
            }
        },
    )

    response = _handle(_payload(), contact_client=contact)

    assert response["statusCode"] == 200
    assert contact.updated == []


# --- 名前フォールバック突合 ------------------------------------------------------------------


def test_handler_matches_existing_contact_by_name_when_company_also_matches() -> None:
    """名前フォールバックは、会社(取引先マスター)が完全一致で特定でき、かつ既存連絡先の
    リンク先と一致する場合のみ採用する（同姓同名の別人誤マージ防止、shirokuma-secレビュー
    WARN対応）。"""
    contact = FakeNotionPageClient(
        pages=[_title_page("existing-1", "名前", "山田太郎")],
        flat_pages={"existing-1": {"携帯番号": None, "取引先マスター": ["cm-1"]}},
    )
    client_master = FakeNotionPageClient(
        pages=[_title_page("cm-1", "取引先名", "株式会社サンプル温泉")]
    )

    response = _handle(_payload(email=""), contact_client=contact, client_master_client=client_master)

    assert json.loads(response["body"])["created"] is False
    assert json.loads(response["body"])["page_id"] == "existing-1"


def test_handler_does_not_use_name_fallback_when_company_does_not_match() -> None:
    """名前は一致しても、会社が特定できない(client_master不一致)場合は別人の可能性が
    高いとみなし、既存連絡先とはマージせず新規作成する。"""
    contact = FakeNotionPageClient(
        pages=[_title_page("existing-1", "名前", "山田太郎")],
        flat_pages={"existing-1": {"携帯番号": None, "取引先マスター": []}},
    )

    response = _handle(_payload(email=""), contact_client=contact)

    body = json.loads(response["body"])
    assert body["created"] is True
    assert contact.updated == []


def test_handler_does_not_use_name_fallback_when_linked_company_differs() -> None:
    """名前は一致するが、既存連絡先が既に別の取引先マスターへリンク済みで今回のcompanyと
    食い違う場合も、別人とみなして新規作成する。"""
    contact = FakeNotionPageClient(
        pages=[_title_page("existing-1", "名前", "山田太郎")],
        flat_pages={"existing-1": {"携帯番号": None, "取引先マスター": ["cm-other"]}},
    )
    client_master = FakeNotionPageClient(
        pages=[_title_page("cm-1", "取引先名", "株式会社サンプル温泉")]
    )

    response = _handle(_payload(email=""), contact_client=contact, client_master_client=client_master)

    body = json.loads(response["body"])
    assert body["created"] is True
    assert contact.updated == []


def test_handler_does_not_use_name_fallback_when_email_provided() -> None:
    """emailがある場合は、email検索がヒットしなければ名前フォールバックへ進むが、
    email自体で既にヒットしていれば名前検索は行わない（クエリの無駄打ちを避ける）。"""
    contact = FakeNotionPageClient(
        pages=[_email_page("existing-1", "yamada@example.com")],
        flat_pages={"existing-1": {"メールアドレス": "yamada@example.com", "携帯番号": None, "取引先マスター": []}},
    )

    response = _handle(_payload(), contact_client=contact)

    assert json.loads(response["body"])["page_id"] == "existing-1"


# --- company空文字時に取引先マスターへ無駄なクエリを投げない --------------------------------


def test_handler_does_not_query_client_master_when_company_blank() -> None:
    client_master = FakeNotionPageClient(pages=[])

    _handle(_payload(company=""), client_master_client=client_master)

    assert client_master.last_filter is None
