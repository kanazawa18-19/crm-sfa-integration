"""Zohoルックアップ由来のリレーションが、変換表に正しく載っていること（2026-08-31）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.webhook_handlers.zoho_field_transforms import (
    SKIP_FIELD,
    ZOHO_LABEL_FIELD_MAPPINGS,
    zoho_action_relation_context,
)

#: Zohoラベル → (db_key, Notionプロパティ名)。実測で値が入っていることを確認済みの6本。
_EXPECTED = [
    ("project", "取引先名", "取引先マスター"),
    ("project", "取引先担当者", "連絡先"),
    ("project", "提案サービス1", "サービス・商品"),
    ("product", "案件", "案件管理"),
    ("action", "チェーン", "👯‍♀️ チェーンリスト"),
    ("chain", "連絡先", "連絡先"),
]


@pytest.mark.parametrize(("db_key", "label", "property_name"), _EXPECTED)
def test_lookup_relations_are_registered(db_key: str, label: str, property_name: str) -> None:
    assert ZOHO_LABEL_FIELD_MAPPINGS[db_key][label][0] == property_name


def test_relation_is_resolved_through_the_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "true")
    store = SQLiteIdMappingStore()
    store.upsert(
        IdMapping(
            notion_key="notion-contact-page",
            db_key="contact",
            zoho_id="777",
            last_synced_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        ),
        expected_last_synced_at=None,
    )
    _property_name, transform = ZOHO_LABEL_FIELD_MAPPINGS["project"]["取引先担当者"]

    with zoho_action_relation_context("zoho-deal-1", {}, None, store):
        assert transform({"name": "渡邊 竜介", "id": "777"}) == "notion-contact-page"


def test_relation_is_skipped_when_the_store_is_not_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解決できないときは、そのプロパティだけ書き込みを見送る（空で上書きしない）。"""
    monkeypatch.setenv("RELATION_SYNC_ENABLED", "true")
    _property_name, transform = ZOHO_LABEL_FIELD_MAPPINGS["project"]["取引先担当者"]

    with zoho_action_relation_context("zoho-deal-1", {}, None, None):
        assert transform({"name": "渡邊 竜介", "id": "777"}) is SKIP_FIELD
