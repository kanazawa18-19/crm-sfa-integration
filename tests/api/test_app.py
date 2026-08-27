from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.document_generation.common import (
    ContractGenerationError,
    DocumentResult,
    TemplateNotFoundError,
)
from src.reports.revenue_target_settings import RevenueTargetSettingsRecord
from src.reports.revenue_target_sheet import RevenueTargetSheetFormatError, RevenueTargetSheetPointer
from src.sync_engine.clients.notion_client import NotionApiError


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


# --- /healthz ------------------------------------------------------------------------------


def test_healthz_returns_200_without_authentication(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_DASHBOARD_API", raising=False)

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- 認証 -------------------------------------------------------------------------------------


def test_protected_endpoint_returns_401_when_token_not_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_DASHBOARD_API", raising=False)

    response = client.get("/api/dashboard/summary")

    assert response.status_code == 401


def test_protected_endpoint_returns_401_with_wrong_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/dashboard/summary", headers={"Authorization": "Bearer wrong-token"}
    )

    assert response.status_code == 401


def test_protected_endpoint_returns_200_with_correct_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    monkeypatch.setattr(
        "src.api.app.build_dashboard_summary", lambda: {"project_count": 0}
    )

    response = client.get(
        "/api/dashboard/summary", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json() == {"project_count": 0}


def test_protected_endpoint_allows_unauthenticated_when_explicitly_enabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_DASHBOARD_API", "true")
    monkeypatch.setattr(
        "src.api.app.build_dashboard_summary", lambda: {"project_count": 0}
    )

    response = client.get("/api/dashboard/summary")

    assert response.status_code == 200


# --- 日付パラメータ -----------------------------------------------------------------------------


def test_daily_report_returns_400_for_invalid_date(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/reports/daily?date=not-a-date",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 400


def test_daily_report_uses_default_date_when_omitted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    captured: dict[str, object] = {}

    def fake_build_daily_report(report_date):
        captured["report_date"] = report_date
        return {"notes": []}

    monkeypatch.setattr("src.api.app.build_daily_report", fake_build_daily_report)

    response = client.get(
        "/api/reports/daily", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert captured["report_date"] is not None


def test_member_performance_returns_400_for_invalid_as_of(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/members/performance?as_of=not-a-date",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 400


# --- CORS -------------------------------------------------------------------------------------


def test_cors_header_present_for_allowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_FRONTEND_ORIGIN", "https://dashboard.example.com")
    # CORSMiddlewareはapp生成時にallow_originsを読み込むため、環境変数設定後にモジュールを再import する。
    import importlib

    import src.api.app as app_module

    importlib.reload(app_module)
    client = TestClient(app_module.app)

    response = client.get(
        "/healthz", headers={"Origin": "https://dashboard.example.com"}
    )

    assert response.headers.get("access-control-allow-origin") == "https://dashboard.example.com"

    # 後続テストへ影響しないよう、CORS未設定状態へ戻して再度reloadしておく。
    monkeypatch.delenv("DASHBOARD_FRONTEND_ORIGIN", raising=False)
    importlib.reload(app_module)


def test_cors_header_absent_for_disallowed_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_FRONTEND_ORIGIN", "https://dashboard.example.com")
    import importlib

    import src.api.app as app_module

    importlib.reload(app_module)
    client = TestClient(app_module.app)

    response = client.get("/healthz", headers={"Origin": "https://evil.example.com"})

    assert "access-control-allow-origin" not in response.headers

    monkeypatch.delenv("DASHBOARD_FRONTEND_ORIGIN", raising=False)
    importlib.reload(app_module)


# --- /api/alerts/manager -------------------------------------------------------------------


def test_manager_alerts_returns_401_when_token_not_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_DASHBOARD_API", raising=False)

    response = client.get("/api/alerts/manager")

    assert response.status_code == 401


def test_manager_alerts_returns_400_for_invalid_as_of(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/alerts/manager?as_of=not-a-date",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 400


def test_manager_alerts_returns_build_manager_alerts_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    captured: dict[str, object] = {}

    def fake_build_manager_alerts(as_of_date):
        captured["as_of"] = as_of_date
        return {
            "as_of": "2026-08-05",
            "alerts": {"lost": [], "lost_candidate": [], "stalled": [], "won": []},
            "counts": {"lost": 0, "lost_candidate": 0, "stalled": 0, "won": 0},
            "stalled_days_threshold": 14,
            "notes": [],
        }

    monkeypatch.setattr("src.api.app.build_manager_alerts", fake_build_manager_alerts)

    response = client.get(
        "/api/alerts/manager", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json()["counts"] == {
        "lost": 0,
        "lost_candidate": 0,
        "stalled": 0,
        "won": 0,
    }
    assert captured["as_of"] is not None


# --- /api/tasks --------------------------------------------------------------------------------


def test_get_tasks_returns_401_when_token_not_set(client: TestClient) -> None:
    response = client.get("/api/tasks")

    assert response.status_code == 401


def test_get_tasks_returns_build_tasks_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_build_tasks() -> dict[str, object]:
        return {"as_of": "2026-08-05", "tasks": [], "overdue_count": 0, "total_count": 0}

    monkeypatch.setattr("src.api.app.build_tasks", fake_build_tasks)

    response = client.get("/api/tasks", headers={"Authorization": "Bearer correct-token"})

    assert response.status_code == 200
    assert response.json() == {
        "as_of": "2026-08-05",
        "tasks": [],
        "overdue_count": 0,
        "total_count": 0,
    }


# --- /api/projects/search --------------------------------------------------------------------


def test_search_projects_returns_401_when_token_not_set(client: TestClient) -> None:
    response = client.get("/api/projects/search?q=サンプル")

    assert response.status_code == 401


def test_search_projects_returns_empty_when_q_blank(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/projects/search?q=", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json() == {"projects": [], "total_matched": 0}


def test_search_projects_returns_matched_projects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_search_projects(query: str) -> dict[str, object]:
        assert query == "サンプル"
        return {
            "projects": [{"notion_page_id": "p1", "project_name": "サンプルホテル"}],
            "total_matched": 1,
        }

    monkeypatch.setattr("src.api.app.search_projects", fake_search_projects)

    response = client.get(
        "/api/projects/search?q=サンプル", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json()["total_matched"] == 1


# --- /api/clients/search -----------------------------------------------------------------------


def test_search_clients_returns_401_when_token_not_set(client: TestClient) -> None:
    response = client.get("/api/clients/search?q=サンプル")

    assert response.status_code == 401


def test_search_clients_returns_empty_when_q_blank(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/clients/search?q=", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json() == {"clients": [], "truncated": False}


def test_search_clients_returns_matched_clients(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_search_clients(query: str) -> dict[str, object]:
        assert query == "サンプル"
        return {
            "clients": [{"notion_page_id": "c1", "取引先名": "サンプルホテル"}],
            "truncated": True,
        }

    monkeypatch.setattr("src.api.app.search_clients", fake_search_clients)

    response = client.get(
        "/api/clients/search?q=サンプル", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json()["truncated"] is True


# --- /api/contacts/search ----------------------------------------------------------------------


def test_search_contacts_returns_401_when_token_not_set(client: TestClient) -> None:
    response = client.get("/api/contacts/search?q=山田")

    assert response.status_code == 401


def test_search_contacts_returns_empty_when_q_blank(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/contacts/search?q=", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json() == {"contacts": [], "truncated": False}


def test_search_contacts_returns_matched_contacts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_search_contacts(query: str) -> dict[str, object]:
        assert query == "山田"
        return {"contacts": [{"notion_page_id": "cnt1", "名前": "山田太郎"}], "truncated": False}

    monkeypatch.setattr("src.api.app.search_contacts", fake_search_contacts)

    response = client.get(
        "/api/contacts/search?q=山田", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json()["truncated"] is False


# --- /api/clients/{client_id}/360 ----------------------------------------------------------------


def test_get_client_360_returns_401_when_token_not_set(client: TestClient) -> None:
    response = client.get("/api/clients/cli-1/360")

    assert response.status_code == 401


def test_get_client_360_returns_404_when_client_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    monkeypatch.setattr("src.api.app.get_client_360", lambda client_id: None)

    response = client.get(
        "/api/clients/cli-1/360", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 404


def test_get_client_360_returns_result_on_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_get_client_360(client_id: str) -> dict[str, object]:
        assert client_id == "cli-1"
        return {
            "client": {"notion_page_id": "cli-1", "取引先名": "サンプルホテル"},
            "projects": [],
            "contacts": [],
            "actions": [],
        }

    monkeypatch.setattr("src.api.app.get_client_360", fake_get_client_360)

    response = client.get(
        "/api/clients/cli-1/360", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json()["client"]["取引先名"] == "サンプルホテル"


# --- /api/documents/generate -------------------------------------------------------------------


def test_generate_document_returns_401_when_token_not_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_DASHBOARD_API", raising=False)

    response = client.get(
        "/api/documents/generate?notion_project_id=abc123&category=見積書"
    )

    assert response.status_code == 401


def test_generate_document_returns_422_for_missing_required_params(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/documents/generate", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 422


def test_generate_document_returns_422_for_invalid_category(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.get(
        "/api/documents/generate?notion_project_id=abc123&category=不明なカテゴリ",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 422


def test_generate_document_passes_overrides_only_for_quote_category(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """手動入力欄(overrides)は見積書カテゴリの場合のみQuoteOverridesとして渡される。"""
    from src.document_generation.quote_generator import QuoteOverrides

    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    captured: dict[str, object] = {}

    def fake_generate_quote(notion_project_id: str, *, overrides: object = None) -> DocumentResult:
        captured["overrides"] = overrides
        return DocumentResult(content=b"x", file_name="x.pdf", mime_type="application/pdf", notes=[])

    monkeypatch.setattr("src.api.app.generate_quote", fake_generate_quote)

    response = client.get(
        "/api/documents/generate"
        "?notion_project_id=abc123&category=見積書"
        "&memo=特記事項&client_name=上書き商店&service_name=リピッテ"
        "&initial_fee=100000&monthly_fee=30000&creator_name=Kanazawa",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    assert captured["overrides"] == QuoteOverrides(
        memo="特記事項",
        client_name="上書き商店",
        service_name="リピッテ",
        initial_fee="100000",
        monthly_fee="30000",
        creator_name="Kanazawa",
    )


def test_generate_document_returns_422_when_template_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_generate_quote(notion_project_id: str, *, overrides: object = None) -> DocumentResult:
        raise TemplateNotFoundError("no template found")

    monkeypatch.setattr("src.api.app.generate_quote", fake_generate_quote)

    response = client.get(
        "/api/documents/generate?notion_project_id=abc123&category=見積書",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 422


def test_generate_document_returns_422_when_contract_placeholder_occurs_unexpected_times(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_generate_contract(notion_project_id: str) -> DocumentResult:
        raise ContractGenerationError("occurrences != 1")

    monkeypatch.setattr("src.api.app.generate_contract", fake_generate_contract)

    response = client.get(
        "/api/documents/generate?notion_project_id=abc123&category=契約書",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 422


def test_generate_document_returns_404_when_notion_page_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_generate_quote(notion_project_id: str, *, overrides: object = None) -> DocumentResult:
        raise NotionApiError(404, "page not found")

    monkeypatch.setattr("src.api.app.generate_quote", fake_generate_quote)

    response = client.get(
        "/api/documents/generate?notion_project_id=does-not-exist&category=見積書",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 404


def test_generate_document_returns_422_for_other_notion_api_errors(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_generate_quote(notion_project_id: str, *, overrides: object = None) -> DocumentResult:
        raise NotionApiError(400, "bad request")

    monkeypatch.setattr("src.api.app.generate_quote", fake_generate_quote)

    response = client.get(
        "/api/documents/generate?notion_project_id=abc123&category=見積書",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 422


def test_generate_document_returns_500_for_unexpected_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_generate_application(notion_project_id: str) -> DocumentResult:
        raise RuntimeError("boom")

    monkeypatch.setattr("src.api.app.generate_application", fake_generate_application)

    response = client.get(
        "/api/documents/generate?notion_project_id=abc123&category=申込書",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 500


def test_generate_document_returns_binary_content_on_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    captured: dict[str, object] = {}

    def fake_generate_contract(notion_project_id: str) -> DocumentResult:
        captured["notion_project_id"] = notion_project_id
        return DocumentResult(
            content=b"fake docx bytes",
            file_name="テスト案件_契約書.docx",
            mime_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            notes=[],
        )

    monkeypatch.setattr("src.api.app.generate_contract", fake_generate_contract)

    response = client.get(
        "/api/documents/generate?notion_project_id=abc123&category=契約書",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    assert response.content == b"fake docx bytes"
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert "%E3%83%86%E3%82%B9%E3%83%88%E6%A1%88%E4%BB%B6" in response.headers["content-disposition"]
    assert captured["notion_project_id"] == "abc123"


def test_generate_document_exposes_notes_via_response_header(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BLOCKER回帰確認: DocumentResult.notes（先頭タブ使用の警告・宛先未反映等、生成物を
    そのまま送付してよいか利用者が判断するための注意事項）がレスポンスに一切含まれず、
    生成結果と一緒に破棄されていた問題の修正確認。
    """
    import json
    from urllib.parse import unquote

    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    expected_notes = [
        "自動生成された書類です。内容を必ず確認してから送付してください。",
        "テンプレートの「福住旅館」タブを複製して使用しました。実案件データが入ったタブを誤って複製していないか確認してください。",
    ]

    def fake_generate_quote(notion_project_id: str, *, overrides: object = None) -> DocumentResult:
        return DocumentResult(
            content=b"fake pdf bytes",
            file_name="テスト案件_見積書.pdf",
            mime_type="application/pdf",
            notes=expected_notes,
        )

    monkeypatch.setattr("src.api.app.generate_quote", fake_generate_quote)

    response = client.get(
        "/api/documents/generate?notion_project_id=abc123&category=見積書",
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    assert "x-document-notes" in response.headers
    decoded_notes = json.loads(unquote(response.headers["x-document-notes"]))
    assert decoded_notes == expected_notes


# --- /api/documents/quote/request-approval ------------------------------------------------------


def test_request_quote_approval_returns_401_when_token_not_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_DASHBOARD_API", raising=False)

    response = client.post(
        "/api/documents/quote/request-approval",
        json={
            "project_id": "abc123",
            "approver_emails": ["approver@example.com"],
            "requested_by_email": "rep@example.com",
        },
    )

    assert response.status_code == 401


def test_request_quote_approval_returns_422_when_drive_not_connected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    from src.document_generation.quote_generator import DriveNotConnectedError

    def fake_request_quote_approval(notion_project_id: str, **kwargs: object) -> None:
        raise DriveNotConnectedError("rep@example.comのDrive連携が未接続です。")

    monkeypatch.setattr("src.api.app.request_quote_approval", fake_request_quote_approval)

    response = client.post(
        "/api/documents/quote/request-approval",
        headers={"Authorization": "Bearer correct-token"},
        json={
            "project_id": "abc123",
            "approver_emails": ["approver@example.com"],
            "requested_by_email": "rep@example.com",
        },
    )

    assert response.status_code == 422
    assert "Drive連携" in response.json()["detail"]


def test_request_quote_approval_returns_422_when_approver_email_invalid(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    from src.document_generation.quote_generator import InvalidApproverEmailError

    def fake_request_quote_approval(notion_project_id: str, **kwargs: object) -> None:
        raise InvalidApproverEmailError("outsider@example.comは承認者として登録されていません。")

    monkeypatch.setattr("src.api.app.request_quote_approval", fake_request_quote_approval)

    response = client.post(
        "/api/documents/quote/request-approval",
        headers={"Authorization": "Bearer correct-token"},
        json={
            "project_id": "abc123",
            "approver_emails": ["outsider@example.com"],
            "requested_by_email": "rep@example.com",
        },
    )

    assert response.status_code == 422
    assert "承認者として登録されていません" in response.json()["detail"]


def test_request_quote_approval_returns_422_when_duplicate_in_progress_request(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    from src.document_generation.quote_generator import DuplicateApprovalRequestError

    def fake_request_quote_approval(notion_project_id: str, **kwargs: object) -> None:
        raise DuplicateApprovalRequestError("この案件の見積書は既に承認リクエストが進行中です。")

    monkeypatch.setattr("src.api.app.request_quote_approval", fake_request_quote_approval)

    response = client.post(
        "/api/documents/quote/request-approval",
        headers={"Authorization": "Bearer correct-token"},
        json={
            "project_id": "abc123",
            "approver_emails": ["approver@example.com"],
            "requested_by_email": "rep@example.com",
        },
    )

    assert response.status_code == 422
    assert "進行中" in response.json()["detail"]


def test_request_quote_approval_returns_500_for_unexpected_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_request_quote_approval(notion_project_id: str, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("src.api.app.request_quote_approval", fake_request_quote_approval)

    response = client.post(
        "/api/documents/quote/request-approval",
        headers={"Authorization": "Bearer correct-token"},
        json={
            "project_id": "abc123",
            "approver_emails": ["approver@example.com"],
            "requested_by_email": "rep@example.com",
        },
    )

    assert response.status_code == 500


def test_request_quote_approval_returns_ids_on_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.document_generation.quote_generator import QuoteApprovalResult

    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    captured: dict[str, object] = {}

    def fake_request_quote_approval(notion_project_id: str, **kwargs: object) -> QuoteApprovalResult:
        captured["notion_project_id"] = notion_project_id
        captured.update(kwargs)
        return QuoteApprovalResult(
            drive_file_id="file-1", drive_approval_id="approval-1", document_approval_id="row-1"
        )

    monkeypatch.setattr("src.api.app.request_quote_approval", fake_request_quote_approval)

    response = client.post(
        "/api/documents/quote/request-approval",
        headers={"Authorization": "Bearer correct-token"},
        json={
            "project_id": "abc123",
            "approver_emails": ["approver@example.com"],
            "requested_by_email": "rep@example.com",
            "message": "ご確認お願いします",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "drive_file_id": "file-1",
        "drive_approval_id": "approval-1",
        "document_approval_id": "row-1",
    }
    from src.document_generation.quote_generator import QuoteOverrides

    assert captured == {
        "notion_project_id": "abc123",
        "approver_emails": ["approver@example.com"],
        "requested_by_email": "rep@example.com",
        "message": "ご確認お願いします",
        "overrides": QuoteOverrides(),
    }


def test_request_quote_approval_passes_multiple_approver_emails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """複数承認者(2026-08-27対応)をリクエストボディに渡した場合、全件が
    request_quote_approval()へそのまま中継されること。"""
    from src.document_generation.quote_generator import QuoteApprovalResult

    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    captured: dict[str, object] = {}

    def fake_request_quote_approval(notion_project_id: str, **kwargs: object) -> QuoteApprovalResult:
        captured.update(kwargs)
        return QuoteApprovalResult(
            drive_file_id="file-1", drive_approval_id="approval-1", document_approval_id="row-1"
        )

    monkeypatch.setattr("src.api.app.request_quote_approval", fake_request_quote_approval)

    response = client.post(
        "/api/documents/quote/request-approval",
        headers={"Authorization": "Bearer correct-token"},
        json={
            "project_id": "abc123",
            "approver_emails": ["a@example.com", "b@example.com"],
            "requested_by_email": "rep@example.com",
        },
    )

    assert response.status_code == 200
    assert captured["approver_emails"] == ["a@example.com", "b@example.com"]


def test_request_quote_approval_passes_overrides(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.document_generation.quote_generator import QuoteApprovalResult, QuoteOverrides

    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    captured: dict[str, object] = {}

    def fake_request_quote_approval(notion_project_id: str, **kwargs: object) -> QuoteApprovalResult:
        captured.update(kwargs)
        return QuoteApprovalResult(
            drive_file_id="file-1", drive_approval_id="approval-1", document_approval_id="row-1"
        )

    monkeypatch.setattr("src.api.app.request_quote_approval", fake_request_quote_approval)

    response = client.post(
        "/api/documents/quote/request-approval",
        headers={"Authorization": "Bearer correct-token"},
        json={
            "project_id": "abc123",
            "approver_emails": ["approver@example.com"],
            "requested_by_email": "rep@example.com",
            "memo": "特記事項",
            "client_name": "上書き商店",
            "service_name": "リピッテ",
            "initial_fee": "100000",
            "monthly_fee": "30000",
            "creator_name": "Kanazawa",
        },
    )

    assert response.status_code == 200
    assert captured["overrides"] == QuoteOverrides(
        memo="特記事項",
        client_name="上書き商店",
        service_name="リピッテ",
        initial_fee="100000",
        monthly_fee="30000",
        creator_name="Kanazawa",
    )


# --- /api/settings/revenue-target-sheet -------------------------------------------------------


def test_get_revenue_target_sheet_settings_returns_401_when_token_not_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_DASHBOARD_API", raising=False)

    response = client.get("/api/settings/revenue-target-sheet")

    assert response.status_code == 401


def test_get_revenue_target_sheet_settings_returns_unconfigured_when_store_not_built(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    monkeypatch.setattr("src.api.app.build_revenue_target_settings_store", lambda: None)

    response = client.get(
        "/api/settings/revenue-target-sheet", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    assert response.json() == {"configured": False, "pointer": None, "updated_at": None}


def test_get_revenue_target_sheet_settings_returns_pointer_when_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    record = RevenueTargetSettingsRecord(
        pointer=RevenueTargetSheetPointer(
            spreadsheet_id="sheet-abc",
            mrr_sheet_name="MRRシート",
            unit_count_sheet_name="販売数シート",
        ),
        updated_at=datetime(2026, 8, 13, 9, 0, 0),
    )

    class FakeStore:
        def get(self):
            return record

    monkeypatch.setattr("src.api.app.build_revenue_target_settings_store", lambda: FakeStore())

    response = client.get(
        "/api/settings/revenue-target-sheet", headers={"Authorization": "Bearer correct-token"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured"] is True
    assert body["pointer"] == {
        "spreadsheet_id": "sheet-abc",
        "mrr_sheet_name": "MRRシート",
        "unit_count_sheet_name": "販売数シート",
    }
    assert body["updated_at"] == "2026-08-13T09:00:00"


def test_post_revenue_target_sheet_settings_returns_401_when_token_not_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DASHBOARD_API_TOKEN", raising=False)
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_DASHBOARD_API", raising=False)

    response = client.post(
        "/api/settings/revenue-target-sheet", json={"spreadsheet_url_or_id": "sheet-abc"}
    )

    assert response.status_code == 401


def test_post_revenue_target_sheet_settings_returns_500_when_store_unconfigured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def _raise():
        raise ValueError("REVENUE_TARGET_SETTINGS_NOTION_DATABASE_ID environment variable is required")

    monkeypatch.setattr("src.api.app.RevenueTargetSettingsStore", _raise)

    response = client.post(
        "/api/settings/revenue-target-sheet",
        json={"spreadsheet_url_or_id": "sheet-abc-1234567890abcdef"},
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 500


def test_post_revenue_target_sheet_settings_extracts_id_from_full_url_and_validates_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    captured: dict[str, object] = {}

    class FakeStore:
        def upsert(self, pointer):
            captured["pointer"] = pointer
            return RevenueTargetSettingsRecord(
                pointer=pointer, updated_at=datetime(2026, 8, 13, 10, 0, 0)
            )

    monkeypatch.setattr("src.api.app.RevenueTargetSettingsStore", lambda: FakeStore())
    monkeypatch.setattr(
        "src.api.app.fetch_mrr_targets",
        lambda spreadsheet_id, sheet_name, **kwargs: {
            date(2026, 6, 1): 100.0,
            date(2026, 7, 1): 200.0,
        },
    )
    monkeypatch.setattr(
        "src.api.app.fetch_unit_count_targets",
        lambda spreadsheet_id, sheet_name, **kwargs: {date(2026, 6, 1): 1},
    )

    response = client.post(
        "/api/settings/revenue-target-sheet",
        json={
            "spreadsheet_url_or_id": (
                "https://docs.google.com/spreadsheets/d/sheet-xyz-1234567890abcdef/edit?gid=0"
            ),
            "mrr_sheet_name": "MRRシート",
            "unit_count_sheet_name": "販売数シート",
        },
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pointer"]["spreadsheet_id"] == "sheet-xyz-1234567890abcdef"
    assert body["validation_success"] is True
    assert body["validation_error"] is None
    assert body["mrr_month_count"] == 2
    assert body["unit_count_month_count"] == 1
    assert captured["pointer"].spreadsheet_id == "sheet-xyz-1234567890abcdef"  # noqa: SLF001


def test_post_revenue_target_sheet_settings_accepts_bare_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    class FakeStore:
        def upsert(self, pointer):
            return RevenueTargetSettingsRecord(
                pointer=pointer, updated_at=datetime(2026, 8, 13, 10, 0, 0)
            )

    monkeypatch.setattr("src.api.app.RevenueTargetSettingsStore", lambda: FakeStore())

    response = client.post(
        "/api/settings/revenue-target-sheet",
        json={"spreadsheet_url_or_id": "  bare-sheet-id-1234567890abcdef  "},
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pointer"]["spreadsheet_id"] == "bare-sheet-id-1234567890abcdef"
    # mrr_sheet_name/unit_count_sheet_nameがどちらも未指定のため、「0ヶ月分」ではなく
    # 「このソースでは追跡しない」を表すNoneのままであること（BLOCKER回帰確認: finding #2。
    # 以前はfetch_all_targets()の戻り値（空dict）からlen()を取っていたため、未設定なのに
    # 誤って0（=「設定済みだが0ヶ月分」）が返っていた）。
    assert body["mrr_month_count"] is None
    assert body["unit_count_month_count"] is None
    assert body["validation_success"] is True


def test_post_revenue_target_sheet_settings_leaves_unit_count_month_count_none_when_unit_sheet_not_configured(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mrr_sheet_nameのみ設定し、unit_count_sheet_nameを未設定のまま保存した場合、
    mrr_month_countは実際に読み込んだ月数、unit_count_month_countはNone
    （「未設定」）のままになること（finding #2の核心となるケース）。"""
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    class FakeStore:
        def upsert(self, pointer):
            return RevenueTargetSettingsRecord(
                pointer=pointer, updated_at=datetime(2026, 8, 13, 10, 0, 0)
            )

    monkeypatch.setattr("src.api.app.RevenueTargetSettingsStore", lambda: FakeStore())
    monkeypatch.setattr(
        "src.api.app.fetch_mrr_targets",
        lambda spreadsheet_id, sheet_name, **kwargs: {date(2026, 6, 1): 100.0},
    )

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("unit_count_sheet_name未設定時にfetch_unit_count_targetsを呼ぶべきではない")

    monkeypatch.setattr("src.api.app.fetch_unit_count_targets", _fail_if_called)

    response = client.post(
        "/api/settings/revenue-target-sheet",
        json={
            "spreadsheet_url_or_id": "sheet-mrr-only-1234567890abcdef",
            "mrr_sheet_name": "MRRシート",
        },
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["mrr_month_count"] == 1
    assert body["unit_count_month_count"] is None


def test_post_revenue_target_sheet_settings_returns_422_for_blank_spreadsheet_input(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.post(
        "/api/settings/revenue-target-sheet",
        json={"spreadsheet_url_or_id": "   "},
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 422


def test_post_revenue_target_sheet_settings_returns_422_for_malformed_spreadsheet_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_extract_spreadsheet_id`の許可リスト検証（WARN: finding #3）。パストラバーサル的な
    値（`../../drive/v3/files`）や短すぎる値は、Notionへ永続化されリクエストURLへ埋め込まれる
    前にここで拒否されること。"""
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.post(
        "/api/settings/revenue-target-sheet",
        json={"spreadsheet_url_or_id": "../../drive/v3/files"},
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 422
    assert "形式が正しくありません" in response.json()["detail"]


def test_post_revenue_target_sheet_settings_returns_422_for_too_short_spreadsheet_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    response = client.post(
        "/api/settings/revenue-target-sheet",
        json={"spreadsheet_url_or_id": "too-short-id"},
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 422


def test_post_revenue_target_sheet_settings_saves_even_when_validation_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """シート形式エラーでも保存自体は成功として扱い、validation_error にメッセージを
    含めて返すこと（設定画面の❌表示用。POSTハンドラのdocstring参照）。"""
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    class FakeStore:
        def upsert(self, pointer):
            return RevenueTargetSettingsRecord(
                pointer=pointer, updated_at=datetime(2026, 8, 13, 10, 0, 0)
            )

    def _raise(spreadsheet_id, sheet_name, **kwargs):
        raise RevenueTargetSheetFormatError("見出しが見つかりませんでした")

    monkeypatch.setattr("src.api.app.RevenueTargetSettingsStore", lambda: FakeStore())
    monkeypatch.setattr("src.api.app.fetch_mrr_targets", _raise)

    response = client.post(
        "/api/settings/revenue-target-sheet",
        json={
            "spreadsheet_url_or_id": "sheet-broken-1234567890abcdef",
            "mrr_sheet_name": "MRRシート",
        },
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["validation_success"] is False
    assert "見出しが見つかりませんでした" in body["validation_error"]
    assert body["pointer"]["spreadsheet_id"] == "sheet-broken-1234567890abcdef"
    # 保存自体は成功しているため、月数フィールドは「例外発生前に読み込めた分」の状態のまま
    # （このテストではmrr側で例外なのでmrr_month_countはNoneのまま、unit_countは未設定のためNone）。
    assert body["mrr_month_count"] is None
    assert body["unit_count_month_count"] is None
