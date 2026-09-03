"""一斉配信エンドポイントの検証（2026-09-03）。

中身の判断は`tests/bulk_email/`・`tests/api/test_bulk_email_service.py`が見ている。
ここで見るのはHTTPの入り口だけ —— 認証が要ること、入力の問題を422で返すこと、
そして**送信のエンドポイントが存在しないこと**。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api.app import app
from src.sync_engine.clients.notion_client import NotionApiError


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _payload(**kwargs: Any) -> dict[str, Any]:
    body = {"subject": "件名", "body": "本文", "sender_name": "金沢", "client_page_ids": ["cli-1"]}
    body.update(kwargs)
    return body


def test_トークンが無ければ401(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    assert client.post("/api/bulk-email/preview", json=_payload()).status_code == 401


def test_プレビューを返す(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")
    monkeypatch.setattr(
        "src.api.routes.bulk_email.build_bulk_email_preview",
        lambda **kwargs: {"sendable": True, "received": kwargs},
    )

    response = client.post(
        "/api/bulk-email/preview",
        json=_payload(),
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 200
    assert response.json()["received"]["client_page_ids"] == ["cli-1"]


def test_入力の問題は422(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def _raise(**kwargs: Any) -> dict[str, Any]:
        raise ValueError("一度に選べる取引先は20社までです")

    monkeypatch.setattr("src.api.routes.bulk_email.build_bulk_email_preview", _raise)

    response = client.post(
        "/api/bulk-email/preview",
        json=_payload(),
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 422
    assert "20社" in response.json()["detail"]


def test_Notionの障害は502(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")

    def _raise(**kwargs: Any) -> dict[str, Any]:
        raise NotionApiError(500, "boom")

    monkeypatch.setattr("src.api.routes.bulk_email.build_bulk_email_preview", _raise)

    response = client.post(
        "/api/bulk-email/preview",
        json=_payload(),
        headers={"Authorization": "Bearer correct-token"},
    )

    assert response.status_code == 502


def _all_paths(routes: object) -> set[str]:
    """`include_router()`したルーターの中まで辿ってパスを集める。

    FastAPIは`app.routes`へルーターをフラットに展開しないため、1段だけ見ると
    「分割したパスが存在しない」ように見える（`test_route_registry.py`の
    `_walk_routes`と同じ理由）。ここを間違えると、**何も検査していないのに緑**になる。
    """
    found: set[str] = set()
    for route in routes or []:  # type: ignore[union-attr]
        path = getattr(route, "path", None)
        if isinstance(path, str):
            found.add(path)
        inner = getattr(route, "original_router", None) or getattr(route, "router", None)
        if inner is not None:
            found |= _all_paths(getattr(inner, "routes", []))
        elif hasattr(route, "routes"):
            found |= _all_paths(route.routes)
    return found


def test_送信のエンドポイントは存在しない() -> None:
    """送信経路が決まるまで作らない、を仕組みで固定する（`docs/bulk_email_design_note.md`）。

    「プレビューだけのつもりが、いつの間にか送れるようになっていた」を防ぐための見張り。
    送信を実装するときは、このテストを書き換えるのが最初の一歩になる。
    """
    bulk_paths = {path for path in _all_paths(app.routes) if path.startswith("/api/bulk-email")}
    # `<=`ではなく`==`。ルートの列挙方法が壊れて空集合になったときに、
    # 「送信エンドポイントは無い」と嘘をつかないようにする。
    assert bulk_paths == {"/api/bulk-email/preview"}, bulk_paths
