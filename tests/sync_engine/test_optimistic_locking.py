"""読んでから書くまでの間に相手が編集していたら上書きしない（2026-08-31）。

Notion の Webhook は発生順に届かず、失敗時は最大8回再送される
（ChatGPTクロスレビュー指摘）。素朴に書くと**古い値で新しい編集を潰す**。
kintone は `revision`、Zoho は `If-Unmodified-Since` で相手側に検出させる。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.db_schema.base import Tool
from src.sync_engine.clients._http import ConcurrentModificationError
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
from src.sync_engine.sync_targets.zoho_sync import ZohoSyncTarget


class _KintoneClient:
    def __init__(self, revision: str | None = "7") -> None:
        self.revision = revision
        self.updates: list[tuple[str, dict[str, Any], str | None]] = []

    def get_record(self, app: str, record_id: str) -> dict[str, Any] | None:
        return {"顧客名": "旧名称", "$revision": self.revision}

    def add_record(self, app: str, record: dict[str, Any]) -> str:
        raise AssertionError("新規作成しない")

    def update_record(
        self, app: str, record_id: str, record: dict[str, Any], *, expected_version=None
    ) -> None:
        self.updates.append((record_id, dict(record), expected_version))


class _ZohoClient:
    def __init__(self) -> None:
        self.updates: list[tuple[str, dict[str, Any], str | None]] = []

    def get_record(self, module: str, record_id: str) -> dict[str, Any] | None:
        return {"Account_Name": "旧名称", "Modified_Time": "2026-08-31T09:00:00+09:00"}

    def insert_record(self, module: str, record: dict[str, Any]) -> str:
        raise AssertionError("新規作成しない")

    def update_record(
        self, module: str, record_id: str, record: dict[str, Any], *, expected_version=None
    ) -> None:
        self.updates.append((record_id, dict(record), expected_version))


def _dispatch(targets: dict[Tool, Any]) -> Any:
    store = SQLiteIdMappingStore()
    store.upsert(
        IdMapping(
            notion_key="page-1",
            db_key="client_master",
            kintone_id="1001",
            zoho_id="zoho-1",
            last_synced_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        ),
        expected_last_synced_at=None,
    )
    dispatcher = Dispatcher(store, targets, sync_system_id="test")
    return dispatcher.dispatch(
        SyncEvent(
            source_tool=Tool.NOTION,
            db_key="client_master",
            external_id="page-1",
            properties={"取引先名": "新名称"},
            occurred_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
    )


def test_kintone_revision_is_sent_with_the_write() -> None:
    client = _KintoneClient(revision="7")
    _dispatch({Tool.KINTONE: KintoneSyncTarget(client, "取引先マスタ")})

    assert client.updates == [("1001", {"顧客名": "新名称"}, "7")]


def test_zoho_modified_time_is_sent_with_the_write() -> None:
    client = _ZohoClient()
    _dispatch({Tool.ZOHO: ZohoSyncTarget(client, "取引先", enabled=True)})

    assert client.updates == [("zoho-1", {"Account_Name": "新名称"}, "2026-08-31T09:00:00+09:00")]


def test_missing_version_still_writes() -> None:
    """版が読めないことを理由に同期を止めない（従来どおりの上書きに戻るだけ）。"""
    client = _KintoneClient(revision=None)
    _dispatch({Tool.KINTONE: KintoneSyncTarget(client, "取引先マスタ")})

    assert client.updates == [("1001", {"顧客名": "新名称"}, None)]


def test_rejected_write_is_reported_as_skipped() -> None:
    """相手に弾かれたら「書けなかった」として扱う。推測で上書きし直さない。"""

    class _Rejecting(_KintoneClient):
        def update_record(self, app, record_id, record, *, expected_version=None) -> None:
            raise ConcurrentModificationError(409, "revision mismatch")

    result = _dispatch({Tool.KINTONE: KintoneSyncTarget(_Rejecting(), "取引先マスタ")})

    assert result.has_partial_skips
    assert Tool.KINTONE in result.properties[0].skipped_tools


def test_version_is_only_attached_to_the_first_write_per_tool() -> None:
    """**同じイベントで同じツールへ2回書くとき、2回目に古い版を送ってはいけない。**

    版はこちらが書くたびに相手側で進む。同じ版を使い回すと、2回目以降が
    「読んだ後に誰かが更新した」と誤判定されて拒否され、その値は再送されないため
    恒久的に反映されないまま残る（shirokuma-secレビューBLOCKER、2026-08-31）。
    「案件名と電話番号を同時に変更」のような日常的な操作で起きる。
    """
    client = _KintoneClient(revision="7")
    store = SQLiteIdMappingStore()
    store.upsert(
        IdMapping(
            notion_key="page-1",
            db_key="client_master",
            kintone_id="1001",
            last_synced_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
        ),
        expected_last_synced_at=None,
    )
    dispatcher = Dispatcher(
        store, {Tool.KINTONE: KintoneSyncTarget(client, "取引先マスタ")}, sync_system_id="test"
    )

    dispatcher.dispatch(
        SyncEvent(
            source_tool=Tool.NOTION,
            db_key="client_master",
            external_id="page-1",
            properties={"取引先名": "新名称", "TEL": "03-1111-2222"},
            occurred_at=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
    )

    versions = [version for _id, _record, version in client.updates]
    assert len(versions) == 2
    assert versions[0] == "7"
    # 2回目は版なし。古い版を送って偽の競合を起こすより、保護を諦める方がまし。
    assert versions[1] is None
