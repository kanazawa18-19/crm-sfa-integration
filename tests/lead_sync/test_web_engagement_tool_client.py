"""WebEngagementToolLeadSyncClientの単体テスト（実HTTP通信はrequests_mockでモック）。"""

from __future__ import annotations

import pytest

from src.lead_sync.web_engagement_tool_client import (
    LeadSyncApiError,
    WebEngagementToolLeadSyncClient,
)


@pytest.fixture
def client() -> WebEngagementToolLeadSyncClient:
    return WebEngagementToolLeadSyncClient(
        base_url="http://localhost:3001", api_token="secret-lead-token"
    )


# --- コンストラクタのバリデーション ---------------------------------------------------------


def test_init_raises_value_error_when_base_url_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEB_ENGAGEMENT_TOOL_URL", raising=False)
    with pytest.raises(ValueError):
        WebEngagementToolLeadSyncClient(base_url=None, api_token="secret-lead-token")


def test_init_raises_value_error_when_api_token_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CRM_SFA_SYNC_API_TOKEN", raising=False)
    with pytest.raises(ValueError):
        WebEngagementToolLeadSyncClient(base_url="http://localhost:3001", api_token=None)


# --- upsert_lead: 正常系 ---------------------------------------------------------------------


def test_upsert_lead_sends_only_email_when_optional_fields_omitted(
    requests_mock, client: WebEngagementToolLeadSyncClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/leads/sync",
        json={"token": "tok-1", "lead_id": "lead-1"},
    )

    result = client.upsert_lead(email="yamada@example.com")

    assert result == {"token": "tok-1", "lead_id": "lead-1"}
    sent_body = requests_mock.request_history[0].json()
    assert sent_body == {"email": "yamada@example.com"}
    assert requests_mock.request_history[0].headers["Authorization"] == "Bearer secret-lead-token"


def test_upsert_lead_sends_all_provided_optional_fields(
    requests_mock, client: WebEngagementToolLeadSyncClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/leads/sync",
        json={"token": "tok-1", "lead_id": "lead-1"},
    )

    client.upsert_lead(
        email="yamada@example.com",
        company="株式会社サンプル",
        last_name="山田太郎",
        phone="090-0000-0000",
    )

    sent_body = requests_mock.request_history[0].json()
    assert sent_body == {
        "email": "yamada@example.com",
        "company": "株式会社サンプル",
        "last_name": "山田太郎",
        "phone": "090-0000-0000",
    }


def test_upsert_lead_omits_first_name_and_assigned_rep_email_when_not_passed(
    requests_mock, client: WebEngagementToolLeadSyncClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/leads/sync",
        json={"token": "tok-1", "lead_id": "lead-1"},
    )

    client.upsert_lead(email="yamada@example.com", first_name=None, assigned_rep_email=None)

    sent_body = requests_mock.request_history[0].json()
    assert "first_name" not in sent_body
    assert "assigned_rep_email" not in sent_body


# --- upsert_lead: エラー ---------------------------------------------------------------------


def test_upsert_lead_raises_lead_sync_api_error_on_401(
    requests_mock, client: WebEngagementToolLeadSyncClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/leads/sync",
        status_code=401,
        json={"error": "unauthorized"},
    )

    with pytest.raises(LeadSyncApiError):
        client.upsert_lead(email="yamada@example.com")


def test_upsert_lead_raises_lead_sync_api_error_on_400(
    requests_mock, client: WebEngagementToolLeadSyncClient
) -> None:
    requests_mock.post(
        "http://localhost:3001/api/leads/sync",
        status_code=400,
        json={"error": "invalid body"},
    )

    with pytest.raises(LeadSyncApiError):
        client.upsert_lead(email="yamada@example.com")


def test_upsert_lead_raises_lead_sync_api_error_on_500(
    requests_mock, client: WebEngagementToolLeadSyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.clients._http.time.sleep", lambda seconds: None)
    requests_mock.post(
        "http://localhost:3001/api/leads/sync",
        status_code=500,
        json={"error": "lead sync failed"},
    )

    with pytest.raises(LeadSyncApiError):
        client.upsert_lead(email="yamada@example.com")
