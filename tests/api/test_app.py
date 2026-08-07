from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.document_generation.common import (
    ContractGenerationError,
    DocumentResult,
    TemplateNotFoundError,
)
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


def test_generate_document_returns_422_when_template_not_found(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def fake_generate_quote(notion_project_id: str) -> DocumentResult:
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

    def fake_generate_quote(notion_project_id: str) -> DocumentResult:
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

    def fake_generate_quote(notion_project_id: str) -> DocumentResult:
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

    def fake_generate_quote(notion_project_id: str) -> DocumentResult:
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
