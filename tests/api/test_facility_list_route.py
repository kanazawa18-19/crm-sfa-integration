"""リスト作成マシーンのAPIルートの検証（src/api/routes/facility_list.py）。

DBにもNotionにも触らない。`build_list`/`count_candidates`/`CrmMatcher`を
差し替えて、ルート層が持つ判断（上限・拒否・履歴の記録）だけを見る。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from src.api import app as app_module
from src.api.routes import facility_list as route
from src.facility_list.application.build_list import ListResult, ListRow
from src.facility_list.domain.models import (
    CrmMatch,
    CrmMatchState,
    Facility,
    FacilityCategory,
)

AUTH = {"Authorization": "Bearer correct-token"}


@pytest.fixture(autouse=True)
def _token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DASHBOARD_API_TOKEN", "correct-token")


@pytest.fixture
def client() -> TestClient:
    return TestClient(app_module.app)


def _row(hotel_no: int, state: CrmMatchState = CrmMatchState.NOT_CHECKED) -> ListRow:
    return ListRow(
        facility=Facility(
            hotel_no=hotel_no,
            name=f"宿{hotel_no}",
            prefecture="鳥取県",
            room_count=30,
            review_average=Decimal("4.0"),
            category=FacilityCategory.RYOKAN,
        ),
        crm=CrmMatch(state=state),
        products=(),
        product_reasons=(),
    )


def _result(rows: list[ListRow]) -> ListResult:
    return ListResult(rows=rows, total_before_crm=len(rows))


def _base_payload(**overrides: Any) -> dict[str, Any]:
    payload = {"prefectures": ["鳥取県"], "room_count_min": 1}
    payload.update(overrides)
    return payload


def _fresh_health():  # noqa: ANN202
    """CRMミラーが新しい状態のダミー。"""
    from datetime import datetime, timezone

    from src.facility_list.infrastructure.db import ClientNameIndexHealth

    return ClientNameIndexHealth(row_count=9914, last_run_at=datetime.now(timezone.utc))


def _export_payload(**overrides: Any) -> dict[str, Any]:
    """書き出しは`user_id`必須(監査ログを必ず残すため)。"""
    payload = _base_payload(user_id="user-1", created_by="金沢")
    payload.update(overrides)
    return payload


class TestAuth:
    def test_トークンが無ければ401(self, client: TestClient) -> None:
        assert client.post("/api/facility-list/preview", json=_base_payload()).status_code == 401
        assert client.post("/api/facility-list/export", json=_base_payload()).status_code == 401


class TestPreview:
    def test_件数と行を返す(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(route, "build_list", lambda criteria: _result([_row(1), _row(2)]))
        response = client.post("/api/facility-list/preview", json=_base_payload(), headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert body["crm_checked"] is False
        assert len(body["rows"]) == 2
        # 行の列数がヘッダーと一致すること（画面が名前で引けるようにするため）。
        assert len(body["rows"][0]) == len(body["headers"])

    def test_先頭だけ返して切ったことを伝える(self, client: TestClient, monkeypatch) -> None:
        rows = [_row(i) for i in range(1, route.PREVIEW_ROWS + 5)]
        monkeypatch.setattr(route, "build_list", lambda criteria: _result(rows))
        body = client.post(
            "/api/facility-list/preview", json=_base_payload(), headers=AUTH
        ).json()
        assert body["total"] == len(rows)
        assert len(body["rows"]) == route.PREVIEW_ROWS
        assert body["truncated"] is True

    def test_CRM条件はプレビューでは断る(self, client: TestClient) -> None:
        # 黙って無視すると、絞れていない件数を見て判断させることになる。
        response = client.post(
            "/api/facility-list/preview",
            json=_base_payload(crm_filter="new_only"),
            headers=AUTH,
        )
        assert response.status_code == 422
        assert "プレビュー" in response.json()["detail"]

    def test_客室数の上下が逆なら422(self, client: TestClient) -> None:
        response = client.post(
            "/api/facility-list/preview",
            json=_base_payload(room_count_min=30, room_count_max=10),
            headers=AUTH,
        )
        assert response.status_code == 422

    def test_未知のカテゴリーは422(self, client: TestClient) -> None:
        response = client.post(
            "/api/facility-list/preview",
            json=_base_payload(categories=["宇宙ステーション"]),
            headers=AUTH,
        )
        assert response.status_code == 422


class _StubMatcher:
    def __init__(self, *, has_notion_access: bool = True, skipped: int = 0) -> None:
        self.has_notion_access = has_notion_access
        self.skipped_by_budget = skipped

    def match_all(self, facilities):  # noqa: ANN001
        return {f.hotel_no: CrmMatch(state=CrmMatchState.NO_NAME_MATCH) for f in facilities}


class TestExport:
    def test_CSVと内訳を返す(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(route, "count_candidates", lambda criteria: 2)
        monkeypatch.setattr(
            route,
            "build_list",
            lambda criteria, matcher=None: _result(
                [_row(1, CrmMatchState.NO_NAME_MATCH), _row(2, CrmMatchState.MATCHED)]
            ),
        )
        monkeypatch.setattr(route, "CrmMatcher", lambda: _StubMatcher())
        monkeypatch.setattr(route.facility_db, "record_list_run", lambda **kwargs: "run-1")

        response = client.post("/api/facility-list/export", json=_export_payload(), headers=AUTH)
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 2
        assert body["new_count"] == 1
        assert body["matched_count"] == 1
        assert body["csv"].startswith("﻿")

    def test_未取引のみを選んでも落ちない(self, client: TestClient, monkeypatch) -> None:
        # 件数チェックにbuild_listを使っていた頃、ここが必ず500になっていた
        # (shirokuma-secレビューのBLOCKER、2026-09-11)。
        monkeypatch.setattr(route, "count_candidates", lambda criteria: 1)
        monkeypatch.setattr(
            route, "build_list", lambda criteria, matcher=None: _result([_row(1)])
        )
        monkeypatch.setattr(route, "CrmMatcher", lambda: _StubMatcher())
        monkeypatch.setattr(route.facility_db, "record_list_run", lambda **kwargs: "run-1")
        monkeypatch.setattr(route.facility_db, "client_name_index_health", _fresh_health)

        response = client.post(
            "/api/facility-list/export",
            json=_export_payload(crm_filter="new_only"),
            headers=AUTH,
        )
        assert response.status_code == 200

    def test_CRMミラーが古ければ取引状態での絞り込みを断る(
        self, client: TestClient, monkeypatch
    ) -> None:
        # 空・古いミラーでも検索は「正常に0件」を返すので、例外では気づけない
        # (ChatGPTレビュー指摘、2026-09-12)。既存顧客が丸ごと新規リストに載る。
        from datetime import datetime, timedelta, timezone

        from src.facility_list.infrastructure.db import ClientNameIndexHealth

        monkeypatch.setattr(
            route.facility_db,
            "client_name_index_health",
            lambda: ClientNameIndexHealth(
                row_count=9914,
                last_run_at=datetime.now(timezone.utc) - timedelta(days=5),
            ),
        )

        def _should_not_be_called():  # noqa: ANN202
            raise AssertionError("ミラーが古いのに突合を始めた")

        monkeypatch.setattr(route, "CrmMatcher", _should_not_be_called)
        response = client.post(
            "/api/facility-list/export",
            json=_export_payload(crm_filter="new_only"),
            headers=AUTH,
        )
        assert response.status_code == 503
        assert "同期が古い" in response.json()["detail"]

    def test_多すぎたらNotionを読む前に断る(self, client: TestClient, monkeypatch) -> None:
        monkeypatch.setattr(route, "count_candidates", lambda criteria: route.MAX_EXPORT_ROWS + 1)

        def _should_not_be_called():  # noqa: ANN202
            raise AssertionError("上限を超えているのにCRM突合を始めた")

        monkeypatch.setattr(route, "CrmMatcher", _should_not_be_called)
        response = client.post("/api/facility-list/export", json=_export_payload(), headers=AUTH)
        assert response.status_code == 422
        assert "上限" in response.json()["detail"]

    def test_Notionを読めないのに提案済み除外を指定したら断る(
        self, client: TestClient, monkeypatch
    ) -> None:
        # 黙って通すと「提案済みを除いたつもりが1件も除かれていない」リストになる。
        monkeypatch.setattr(route, "count_candidates", lambda criteria: 1)
        monkeypatch.setattr(route, "CrmMatcher", lambda: _StubMatcher(has_notion_access=False))
        response = client.post(
            "/api/facility-list/export",
            json=_export_payload(exclude_proposed_services=["フルスコ"]),
            headers=AUTH,
        )
        assert response.status_code == 503

    def test_履歴を残す(self, client: TestClient, monkeypatch) -> None:
        recorded: dict[str, Any] = {}
        monkeypatch.setattr(route, "count_candidates", lambda criteria: 1)
        monkeypatch.setattr(
            route, "build_list", lambda criteria, matcher=None: _result([_row(1)])
        )
        monkeypatch.setattr(route, "CrmMatcher", lambda: _StubMatcher())
        monkeypatch.setattr(
            route.facility_db,
            "record_list_run",
            lambda **kwargs: (recorded.update(kwargs), "run-1")[1],
        )

        client.post("/api/facility-list/export", json=_export_payload(), headers=AUTH)
        assert recorded["user_id"] == "user-1"
        assert recorded["criteria"]["prefectures"] == ["鳥取県"]
        # 実行者の情報を条件として保存し直さない（二重に持たない）。
        assert "user_id" not in recorded["criteria"]

    def test_user_idが無ければ書き出さない(self, client: TestClient, monkeypatch) -> None:
        # 空を通していた頃は、APIを直接叩けば監査ログ無しで個人情報CSVを出せた
        # (ChatGPTレビューのBLOCKER、2026-09-12)。
        def _should_not_be_called(**kwargs):  # noqa: ANN003, ANN202
            raise AssertionError("user_idが無いのに履歴を書こうとした")

        monkeypatch.setattr(route.facility_db, "record_list_run", _should_not_be_called)
        response = client.post("/api/facility-list/export", json=_base_payload(), headers=AUTH)
        assert response.status_code == 422

    def test_履歴を残せなければ書き出しごと断る(self, client: TestClient, monkeypatch) -> None:
        # 「記録は無いがCSVは出た」を許すと監査ログの意味が無くなる。
        monkeypatch.setattr(route, "count_candidates", lambda criteria: 1)
        monkeypatch.setattr(
            route, "build_list", lambda criteria, matcher=None: _result([_row(1)])
        )
        monkeypatch.setattr(route, "CrmMatcher", lambda: _StubMatcher())
        monkeypatch.setattr(route.facility_db, "record_list_run", lambda **kwargs: None)
        response = client.post("/api/facility-list/export", json=_export_payload(), headers=AUTH)
        assert response.status_code == 503

    def test_打ち切った件数を返す(self, client: TestClient, monkeypatch) -> None:
        # 時間予算を使い切って突合できなかった分を黙って隠さない。
        monkeypatch.setattr(route, "count_candidates", lambda criteria: 1)
        monkeypatch.setattr(
            route, "build_list", lambda criteria, matcher=None: _result([_row(1)])
        )
        monkeypatch.setattr(route, "CrmMatcher", lambda: _StubMatcher(skipped=7))
        monkeypatch.setattr(route.facility_db, "record_list_run", lambda **kwargs: "run-1")
        body = client.post(
            "/api/facility-list/export", json=_export_payload(), headers=AUTH
        ).json()
        assert body["unchecked_count"] == 7
