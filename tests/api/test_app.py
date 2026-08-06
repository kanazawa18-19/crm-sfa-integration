from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import app


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
