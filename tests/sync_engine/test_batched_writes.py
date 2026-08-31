"""1イベント・1ツールにつき1回だけ書く（2026-09-01）。

以前はプロパティごとにAPIを叩いていた。5項目なら同じレコードへ5回。
呼び出し回数の無駄だけでなく、**版（楽観的排他）が書くたびに進むため2つ目以降で
偽の競合を起こす**という不具合の温床でもあった。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_targets.zoho_sync import ZohoSyncTarget

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


class _ZohoClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any], str | None]] = []
        self.get_calls = 0

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        self.get_calls += 1
        return {"Modified_Time": "2026-09-01T09:00:00+09:00"}

    def insert_record(self, module: str, record: dict[str, Any]) -> str:
        raise AssertionError("新規作成しない")

    def update_record(
        self, module: str, record_id: str, record: dict[str, Any], *, expected_version=None
    ) -> None:
        self.updates.append((record_id, dict(record), expected_version))


def _dispatch(client: _ZohoClient, properties: dict[str, Any]) -> Any:
    store = SQLiteIdMappingStore()
    store.upsert(
        IdMapping(
            notion_key="page-1",
            db_key="project",
            zoho_id="zoho-1",
            last_synced_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        ),
        expected_last_synced_at=None,
    )
    dispatcher = Dispatcher(
        store, {Tool.ZOHO: ZohoSyncTarget(client, "案件", enabled=True)}, sync_system_id="test"
    )
    return dispatcher.dispatch(
        SyncEvent(
            source_tool=Tool.NOTION,
            db_key="project",
            external_id="page-1",
            properties=properties,
            occurred_at=NOW,
        )
    )


def test_four_properties_become_one_api_call() -> None:
    client = _ZohoClient()

    _dispatch(
        client,
        {
            "案件名": "A社 リピッテ",
            "メモ": "商談メモ",
            "ネックポイント": "予算",
            "電話番号": "03-1111-2222",
        },
    )

    assert len(client.updates) == 1
    _record_id, record, _version = client.updates[0]
    assert record == {
        "Deal_Name": "A社 リピッテ",
        "field70": "商談メモ",
        "field15": "予算",
        "field7": "03-1111-2222",
    }


def test_unsendable_properties_are_reported_per_property() -> None:
    """まとめて書いても「どの項目が落ちたか」は個別に分かること。

    `確度`はZoho側に対応する項目が無いので送れない。`案件名`は送れる。
    """
    client = _ZohoClient()

    result = _dispatch(client, {"案件名": "A社", "確度": "A"})

    by_name = {p.property_name: p for p in result.properties}
    assert Tool.ZOHO in by_name["案件名"].written_tools
    assert Tool.ZOHO in by_name["確度"].skipped_tools
    # 送れる項目だけが1回で送られること。
    assert len(client.updates) == 1
    assert client.updates[0][1] == {"Deal_Name": "A社"}


def test_no_call_at_all_when_nothing_can_be_sent() -> None:
    """1項目も送れないなら、APIを叩かない（空のレコードで更新しない）。"""
    client = _ZohoClient()

    result = _dispatch(client, {"確度": "A"})

    assert client.updates == []
    assert Tool.ZOHO in result.properties[0].skipped_tools
