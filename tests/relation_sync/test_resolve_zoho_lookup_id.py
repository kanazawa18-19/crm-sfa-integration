"""Zohoのルックアップ項目 → Notionリレーション（IDマッピング経由、2026-08-31）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.relation_sync.resolve_zoho import resolve_zoho_relation_by_lookup_id
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore

_LOOKUP = {"name": "UNOHOTEL", "id": "22334000000692771"}


@pytest.fixture
def store() -> SQLiteIdMappingStore:
    store = SQLiteIdMappingStore()
    store.upsert(
        IdMapping(
            notion_key="notion-client-page",
            db_key="client_master",
            zoho_id="22334000000692771",
            last_synced_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        ),
        expected_last_synced_at=None,
    )
    return store


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "true")


def _resolve(value: object, store: object, target: str = "client_master") -> str | None:
    return resolve_zoho_relation_by_lookup_id(
        value,
        target_db_key=target,
        property_name="取引先マスター",
        id_mapping_store=store,
        source_record_id="zoho-1",
    )


def test_resolves_via_id_mapping_without_name_matching(store: SQLiteIdMappingStore) -> None:
    """ルックアップにはZohoレコードidが入っているので、会社名の突き合わせは要らない。"""
    assert _resolve(_LOOKUP, store) == "notion-client-page"


def test_name_is_irrelevant_to_the_result(store: SQLiteIdMappingStore) -> None:
    """名前が全く違っても、idが合っていれば同じページに解決すること（名寄せしていない証拠）。"""
    assert _resolve({"name": "まったく別の表記", "id": "22334000000692771"}, store) == (
        "notion-client-page"
    )


def test_returns_none_when_the_target_is_not_synced_yet(
    store: SQLiteIdMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """相手がまだNotionに無いときは、推測で別ページに紐付けずNoneを返すこと。"""
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(
        "src.relation_sync.resolve_zoho.enqueue_for_review",
        lambda **kwargs: recorded.append(kwargs),
    )

    assert _resolve({"name": "未同期ホテル", "id": "99999999"}, store) is None
    # 人が気づけるようにレビューキューへ積む。
    assert recorded and recorded[0]["raw_value"] == "未同期ホテル"


def test_looks_up_in_the_target_database_only(store: SQLiteIdMappingStore) -> None:
    """db_keyをまたいで誤って引かないこと（Zohoのidはモジュールごとに独立）。"""
    assert _resolve(_LOOKUP, store, target="project") is None


def test_returns_none_for_a_plain_string_value(store: SQLiteIdMappingStore) -> None:
    """idを持たない値（文字列）では解決できない。名寄せ経路に任せる。"""
    assert _resolve("UNOHOTEL", store) is None


def test_returns_none_without_a_store(store: SQLiteIdMappingStore) -> None:
    assert _resolve(_LOOKUP, None) is None


def test_does_nothing_when_relation_sync_is_disabled(
    store: SQLiteIdMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "false")

    assert _resolve(_LOOKUP, store) is None


def test_review_queue_failure_does_not_break_the_sync(
    store: SQLiteIdMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """記録に失敗しても同期本体は止めない。"""

    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("DB down")

    monkeypatch.setattr("src.relation_sync.resolve_zoho.enqueue_for_review", _boom)

    assert _resolve({"name": "未同期ホテル", "id": "99999999"}, store) is None
