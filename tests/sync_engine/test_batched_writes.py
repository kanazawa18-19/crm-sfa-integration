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


# --- 外部発（kintone/Zoho発）の経路 ---------------------------------------------------------
#
# Notion発とは別のコードパス（競合解決を通り、`decided`へ貯めてからまとめる）。
# ここにテストが無く、`_group_by_tool`の値取り違えを混入させても検出できなかった
# （kuma-qaレビューBLOCKER、2026-09-01）。

from src.db_schema.base import PropertyType  # noqa: E402
from src.sync_engine.clients._http import ConcurrentModificationError  # noqa: E402
from tests.sync_engine.test_dispatcher import (  # noqa: E402
    FakeSyncTarget,
    NOTION_LAST_EDITED_TIME_KEY,
    _all_targets,
)


def _external_dispatch(targets: dict[Tool, Any], properties: dict[str, Any]) -> Any:
    store = SQLiteIdMappingStore()
    store.upsert(
        IdMapping(
            notion_key="CLI-001",
            db_key="client_master",
            kintone_id="1001",
            zoho_id="zoho-1",
            spreadsheet_row=5,
            last_synced_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        ),
        expected_last_synced_at=None,
    )
    return Dispatcher(store, targets).dispatch(
        SyncEvent(
            source_tool=Tool.KINTONE,
            db_key="client_master",
            external_id="1001",
            properties=properties,
            occurred_at=NOW,
        )
    )


def _stale(**values: Any) -> dict[str, Any]:
    return {**values, "updated_at": datetime(2026, 8, 30, tzinfo=timezone.utc)}


def test_external_event_writes_each_tool_once_with_all_properties() -> None:
    """kintone発でも、1ツールにつき1回・全項目まとめて書くこと。"""
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={
            "CLI-001": {
                "取引先名": "旧名称",
                "住所": "旧住所",
                NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 8, 30, tzinfo=timezone.utc),
            }
        },
    )
    zoho = FakeSyncTarget(Tool.ZOHO, records={"zoho-1": _stale(取引先名="旧名称", 住所="旧住所")})
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = zoho

    _external_dispatch(targets, {"取引先名": "新名称", "住所": "新住所"})

    for target, external_id in ((notion, "CLI-001"), (zoho, "zoho-1")):
        assert len(target.upsert_calls) == 1, target.tool
        sent_id, sent = target.upsert_calls[0]
        assert sent_id == external_id
        # **値の取り違えが起きていないこと。** ここが無いと _group_by_tool のバグを見逃す。
        assert sent == {"取引先名": "新名称", "住所": "新住所"}


def test_no_op_property_is_not_mixed_into_the_write() -> None:
    """一致していて書く必要のない項目が、書き込みに紛れ込まないこと。"""
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={
            "CLI-001": {
                "取引先名": "旧名称",
                "住所": "同じ住所",
                NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 8, 30, tzinfo=timezone.utc),
            }
        },
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion

    result = _external_dispatch(targets, {"取引先名": "新名称", "住所": "同じ住所"})

    assert len(notion.upsert_calls) == 1
    _sent_id, sent = notion.upsert_calls[0]
    assert sent == {"取引先名": "新名称"}
    # 一致していた項目も報告には残る（書き込み対象ゼロとして）。
    assert {p.property_name for p in result.properties} == {"取引先名", "住所"}


def test_rejected_write_skips_every_property_for_that_tool() -> None:
    """まとめて1回なので、拒否されたらそのツールの**全項目**がスキップになること。"""

    class _Rejecting(FakeSyncTarget):
        def upsert_record(self, external_id, properties, *, db_key=None, expected_version=None):
            raise ConcurrentModificationError(409, "revision mismatch")

    notion = FakeSyncTarget(
        Tool.NOTION,
        records={
            "CLI-001": {
                "取引先名": "旧名称",
                "住所": "旧住所",
                NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 8, 30, tzinfo=timezone.utc),
            }
        },
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = _Rejecting(
        Tool.ZOHO, records={"zoho-1": _stale(取引先名="旧名称", 住所="旧住所")}
    )

    result = _external_dispatch(targets, {"取引先名": "新名称", "住所": "新住所"})

    for prop in result.properties:
        assert Tool.ZOHO in prop.skipped_tools, prop.property_name


def test_falls_back_to_sending_everything_when_the_question_fails() -> None:
    """「送れない項目は何か」を聞けなくても同期を止めない。そのまま送る。"""

    class _Broken(FakeSyncTarget):
        def unsupported_properties(self, properties, *, db_key=None):
            raise RuntimeError("問い合わせ失敗")

    notion = FakeSyncTarget(
        Tool.NOTION,
        records={
            "CLI-001": {
                "取引先名": "旧名称",
                NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 8, 30, tzinfo=timezone.utc),
            }
        },
    )
    broken = _Broken(Tool.ZOHO, records={"zoho-1": _stale(取引先名="旧名称")})
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = broken

    _external_dispatch(targets, {"取引先名": "新名称"})

    assert len(broken.upsert_calls) == 1
    assert broken.upsert_calls[0][1] == {"取引先名": "新名称"}


def test_each_tool_gets_only_the_properties_meant_for_it() -> None:
    """プロパティごとに送り先が違うとき、混ざらないこと。

    `取引先ID`はNotionだけ（`sync_scope=INTERNAL`相当の扱い）、`取引先名`は全ツールへ。
    まとめて書くようにしたので、ここが混ざると別ツールへ余計な項目を送ってしまう。
    """
    from src.db_schema.registry import get_schema

    schema = get_schema("client_master")
    notion_only = [
        p.name
        for p in schema.properties
        if p.property_type is PropertyType.TEXT and not p.should_sync_to(Tool.ZOHO)
    ]
    if not notion_only:
        return  # 対象が無ければ検証不要

    notion = FakeSyncTarget(
        Tool.NOTION,
        records={
            "CLI-001": {
                "取引先名": "旧名称",
                notion_only[0]: "旧値",
                NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 8, 30, tzinfo=timezone.utc),
            }
        },
    )
    zoho = FakeSyncTarget(Tool.ZOHO, records={"zoho-1": _stale(取引先名="旧名称")})
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = zoho

    _external_dispatch(targets, {"取引先名": "新名称", notion_only[0]: "新値"})

    # Zohoへは「取引先名」だけ。Notion専用の項目が紛れ込まないこと。
    assert len(zoho.upsert_calls) == 1
    assert notion_only[0] not in zoho.upsert_calls[0][1]
