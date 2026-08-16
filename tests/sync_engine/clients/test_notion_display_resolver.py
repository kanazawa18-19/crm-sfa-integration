from __future__ import annotations

import pytest

from src.sync_engine.clients.notion_display_resolver import resolve_display_values

DB_KEY = "client_master"


@pytest.fixture(autouse=True)
def _notion_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_API_KEY", "secret-notion-key")


def test_passes_through_non_relation_non_user_values() -> None:
    resolved = resolve_display_values(
        DB_KEY, {"取引先名": "株式会社サンプル", "顧客種別": "ホテル・旅館"}, actor_source="kintone_webhook"
    )

    assert resolved == {"取引先名": "株式会社サンプル", "顧客種別": "ホテル・旅館"}


def test_passes_through_none_values() -> None:
    resolved = resolve_display_values(DB_KEY, {"チェーン": None}, actor_source="kintone_webhook")

    assert resolved == {"チェーン": None}


def test_passes_through_unknown_property_names() -> None:
    resolved = resolve_display_values(DB_KEY, {"未知のプロパティ": "value"}, actor_source="kintone_webhook")

    assert resolved == {"未知のプロパティ": "value"}


def test_resolves_relation_ids_to_target_page_titles(requests_mock) -> None:
    requests_mock.get(
        "https://api.notion.com/v1/pages/chain-page-1",
        json={
            "id": "chain-page-1",
            "properties": {"グループ名": {"type": "title", "title": [{"plain_text": "サンプルチェーン"}]}},
        },
    )

    resolved = resolve_display_values(
        DB_KEY, {"チェーン": ["chain-page-1"]}, actor_source="kintone_webhook"
    )

    assert resolved == {"チェーン": ["サンプルチェーン"]}


def test_relation_falls_back_to_raw_id_when_target_page_fetch_fails(requests_mock) -> None:
    requests_mock.get("https://api.notion.com/v1/pages/chain-page-1", status_code=500)

    resolved = resolve_display_values(
        DB_KEY, {"チェーン": ["chain-page-1"]}, actor_source="kintone_webhook"
    )

    assert resolved == {"チェーン": ["chain-page-1"]}


def test_relation_falls_back_to_raw_id_when_target_page_not_found(requests_mock) -> None:
    requests_mock.get("https://api.notion.com/v1/pages/chain-page-1", status_code=404)

    resolved = resolve_display_values(
        DB_KEY, {"チェーン": ["chain-page-1"]}, actor_source="kintone_webhook"
    )

    assert resolved == {"チェーン": ["chain-page-1"]}


def test_resolves_user_ids_to_names(requests_mock) -> None:
    requests_mock.get(
        "https://api.notion.com/v1/users",
        json={"results": [{"id": "user-1", "name": "山田太郎"}], "has_more": False},
    )

    resolved = resolve_display_values("project", {"担当メンバー": ["user-1"]}, actor_source="kintone_webhook")

    assert resolved == {"担当メンバー": ["山田太郎"]}


def test_user_falls_back_to_raw_id_when_directory_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTION_API_KEY", raising=False)

    resolved = resolve_display_values("project", {"担当メンバー": ["user-1"]}, actor_source="kintone_webhook")

    assert resolved == {"担当メンバー": ["user-1"]}


def test_skips_resolution_entirely_for_migration_actor(requests_mock) -> None:
    """一括移行(148,000件規模)でNotion APIリクエスト数を増やさないため、
    actorSource="migration"の場合は解決自体をスキップする(モジュールdocstring参照)。"""
    resolved = resolve_display_values(DB_KEY, {"チェーン": ["chain-page-1"]}, actor_source="migration")

    assert resolved == {"チェーン": ["chain-page-1"]}
    assert requests_mock.call_count == 0
