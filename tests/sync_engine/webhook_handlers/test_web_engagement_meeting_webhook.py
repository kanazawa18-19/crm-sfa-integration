from __future__ import annotations

import json
from typing import Any

import pytest

from src.sync_engine.webhook_handlers._common import WEBHOOK_SECRET_HEADER
from src.sync_engine.webhook_handlers.web_engagement_meeting_webhook import handler


class FakeContactClient:
    def __init__(self, contacts_by_email: dict[str, dict[str, Any]] | None = None) -> None:
        self._by_email = contacts_by_email or {}
        self._by_id = {page["id"]: page for page in self._by_email.values()}

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        email = filter["email"]["equals"]  # type: ignore[index]
        page = self._by_email.get(email)
        return [page] if page else []

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        return self._by_id[page_id]


class FakeProjectClient:
    def __init__(
        self,
        projects_by_client_master: dict[str, list[dict[str, Any]]] | None = None,
        pages_by_id: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._by_client_master = projects_by_client_master or {}
        self._pages_by_id = pages_by_id or {}

    def query_all_pages(self, *, filter: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        client_master_id = filter["relation"]["contains"]  # type: ignore[index]
        return self._by_client_master.get(client_master_id, [])

    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        return self._pages_by_id[page_id]


def _contact_page(page_id: str, email: str, client_master_ids: list[str]) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "メールアドレス": {"type": "email", "email": email},
            "取引先マスター": {
                "type": "relation",
                "relation": [{"id": cid} for cid in client_master_ids],
            },
        },
    }


def _project_page(page_id: str, status: str, name: str = "テスト案件") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "営業ステータス": {"type": "status", "status": {"name": status}},
            "案件名": {"type": "title", "title": [{"plain_text": name}]},
        },
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    body = {
        "google_event_id": "event-1@google.com",
        "title": "【商談（訪問）】〇〇ホテル様",
        "starts_at": "2026-08-12T10:00:00+09:00",
        "attendee_emails": ["yamada@example.com", "sales@cnctor.jp"],
        "meet_link": None,
        "rep_email": "sales@cnctor.jp",
    }
    body.update(overrides)
    return body


def _event(payload: dict[str, Any], *, secret: str | None = "correct-secret") -> dict[str, Any]:
    headers = {WEBHOOK_SECRET_HEADER: secret} if secret is not None else {}
    return {"body": json.dumps(payload), "headers": headers}


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEB_ENGAGEMENT_MEETING_WEBHOOK_SECRET", "correct-secret")
    monkeypatch.setenv("INTERNAL_EMAIL_DOMAINS", "cnctor.jp")
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.web_engagement_meeting_webhook.post_approval_request",
        lambda candidate: None,
    )


def test_handler_returns_401_when_secret_mismatches() -> None:
    response = handler(
        _event(_payload(), secret="wrong-secret"),
        context=None,
        contact_client=FakeContactClient(),
        project_client=FakeProjectClient(),
    )
    assert response["statusCode"] == 401


def test_handler_returns_400_when_required_field_missing() -> None:
    payload = _payload()
    del payload["google_event_id"]

    response = handler(
        _event(payload), context=None, contact_client=FakeContactClient(), project_client=FakeProjectClient()
    )

    assert response["statusCode"] == 400


def test_handler_posts_approval_request_when_project_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def _fake_post(candidate: Any) -> bool:
        calls.append(candidate)
        return True

    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.web_engagement_meeting_webhook.post_approval_request",
        _fake_post,
    )
    contact = FakeContactClient(
        {"yamada@example.com": _contact_page("contact-1", "yamada@example.com", ["client-1"])}
    )
    project = FakeProjectClient(
        {"client-1": [_project_page("project-1", "口頭受注")]},
        pages_by_id={"project-1": _project_page("project-1", "口頭受注", name="〇〇ホテル様導入案件")},
    )

    response = handler(_event(_payload()), context=None, contact_client=contact, project_client=project)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["posted"] is True
    assert len(calls) == 1
    assert calls[0].project_page_id == "project-1"
    assert calls[0].project_name == "〇〇ホテル様導入案件"
    assert calls[0].rep_email == "sales@cnctor.jp"
    # sales@cnctor.jp（社内ドメイン）はattendee_displayに含まれない
    assert "sales@cnctor.jp" not in calls[0].attendee_display


def test_handler_does_not_post_when_no_unique_project_match(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []
    monkeypatch.setattr(
        "src.sync_engine.webhook_handlers.web_engagement_meeting_webhook.post_approval_request",
        lambda candidate: calls.append(candidate),
    )
    contact = FakeContactClient({})
    project = FakeProjectClient({})

    response = handler(_event(_payload()), context=None, contact_client=contact, project_client=project)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["posted"] is False
    assert calls == []
