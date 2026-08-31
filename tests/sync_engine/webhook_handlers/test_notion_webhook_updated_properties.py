"""Notion Webhookの「変更されたプロパティ」への絞り込み（2026-08-31）。

ページ全体を外部へ流すと、実際には触られていない項目まで伝播対象になり、
外部ツール側で後から入った値を、Notionに残っていた古い値で上書きしてしまう。
"""

from __future__ import annotations

import json
from typing import Any

from src.sync_engine.webhook_handlers.notion_webhook import fetch_and_normalize_notion_page

_PAGE: dict[str, Any] = {
    "id": "page-1",
    "parent": {"database_id": "db-1"},
    "last_edited_time": "2026-08-31T09:00:00.000Z",
    "properties": {
        "案件名": {"id": "title", "type": "title", "title": [{"plain_text": "A社"}]},
        "電話番号": {"id": "abcd", "type": "phone_number", "phone_number": "03-0000-0000"},
    },
}


class _FakeClient:
    def get_raw_page(self, page_id: str) -> dict[str, Any]:
        return json.loads(json.dumps(_PAGE))


def test_only_changed_properties_are_kept() -> None:
    payload = fetch_and_normalize_notion_page("page-1", _FakeClient(), ["title"])

    assert list(payload["properties"]) == ["案件名"]


def test_all_properties_are_kept_when_nothing_is_specified() -> None:
    """ページ作成イベント等、変更プロパティが分からない場合は従来どおり全部通す。"""
    payload = fetch_and_normalize_notion_page("page-1", _FakeClient(), None)

    assert set(payload["properties"]) == {"案件名", "電話番号"}


def test_falls_back_to_all_properties_when_nothing_matches() -> None:
    """IDの形式が想定と違うとき、何も同期されない状態へ静かに倒れないこと。"""
    payload = fetch_and_normalize_notion_page("page-1", _FakeClient(), ["未知のID"])

    assert set(payload["properties"]) == {"案件名", "電話番号"}
