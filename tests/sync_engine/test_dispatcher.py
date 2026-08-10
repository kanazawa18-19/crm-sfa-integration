"""dispatcherのSelf-Exclusion・無限ループ防止・sync_scope判定・コンフリクト経由の書き込みを検証する。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
    Tool,
)
from src.sync_engine.conflict_resolver import RejectedData, ResolutionAction
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_targets.base import SyncTarget
from src.sync_engine.sync_targets.spreadsheet_sync import SYNC_LOG_SHEET_NAME, SpreadsheetSyncTarget

NOW = datetime(2026, 8, 5, 9, 0, 0, tzinfo=timezone.utc)


class FakeSyncTarget(SyncTarget):
    """テスト用のインメモリSyncTarget。get_recordの固定値・upsert呼び出し履歴を保持する。

    `always_skip=True`にすると、`_MultiDb*SyncTarget`（本番用ルーター、
    `src/sync_engine/production_wiring.py`）が外部IDからdb_keyを解決できず実際には
    書き込まなかったケース等を模して、`upsert_record()`がNone（`SyncTarget`の契約上
    「実際には書き込まれなかった」を表す）を返すようにする。
    """

    def __init__(
        self, tool: Tool, records: dict[str, dict[str, Any]] | None = None, *, always_skip: bool = False
    ) -> None:
        self.tool = tool
        self._records = records or {}
        self._always_skip = always_skip
        self.upsert_calls: list[tuple[str | None, dict[str, Any]]] = []
        self.delete_calls: list[str] = []

    def get_record(self, external_id: str) -> dict[str, Any] | None:
        return self._records.get(external_id)

    def upsert_record(self, external_id: str | None, properties: dict[str, Any]) -> str | None:
        self.upsert_calls.append((external_id, dict(properties)))
        if self._always_skip:
            return None
        return external_id or "new-id"

    def delete_record(self, external_id: str) -> None:
        self.delete_calls.append(external_id)


@pytest.fixture
def store() -> SQLiteIdMappingStore:
    s = SQLiteIdMappingStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def mapping(store: SQLiteIdMappingStore) -> IdMapping:
    m = IdMapping(
        notion_key="CLI-001",
        db_key="client_master",
        kintone_id="1001",
        zoho_id="zoho-1",
        spreadsheet_row=5,
        last_synced_at=NOW - timedelta(days=1),
    )
    store.upsert(m)
    return m


def _all_targets() -> dict[Tool, FakeSyncTarget]:
    return {
        Tool.NOTION: FakeSyncTarget(Tool.NOTION),
        Tool.KINTONE: FakeSyncTarget(Tool.KINTONE),
        Tool.ZOHO: FakeSyncTarget(Tool.ZOHO),
        Tool.SPREADSHEET: FakeSyncTarget(Tool.SPREADSHEET),
    }


class FakeSpreadsheetClient:
    """SpreadsheetSyncTarget.append_conflict_log の実際の呼び出しを検証するための最小Fake。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[int, dict[str, Any]]] = {}
        self._next_row: dict[str, int] = {}

    def get_row(self, sheet: str, row: int) -> dict[str, Any] | None:
        return self.rows.get(sheet, {}).get(row)

    def append_row(self, sheet: str, values: dict[str, Any]) -> int:
        row = self._next_row.get(sheet, 0) + 1
        self._next_row[sheet] = row
        self.rows.setdefault(sheet, {})[row] = dict(values)
        return row

    def update_row(self, sheet: str, row: int, values: dict[str, Any]) -> None:
        self.rows.setdefault(sheet, {}).setdefault(row, {}).update(values)


class SpyNotifier:
    """SlackNotifier.notify_conflict の呼び出し内容を記録するテスト用スタブ。"""

    def __init__(self) -> None:
        self.notified: list[RejectedData] = []

    def notify_conflict(self, rejected: RejectedData) -> None:
        self.notified.append(rejected)


# --- 無限ループ防止 -------------------------------------------------------------------


def test_dispatch_skips_own_system_event(store: SQLiteIdMappingStore, mapping: IdMapping) -> None:
    dispatcher = Dispatcher(store, _all_targets(), sync_system_id="自社CRM-Engine")
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
        sync_system_id="自社CRM-Engine",
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "own_system_event"


def test_dispatch_processes_event_from_other_origin(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    dispatcher = Dispatcher(store, _all_targets(), sync_system_id="自社CRM-Engine")
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={},
        sync_system_id="別の何か",
    )

    result = dispatcher.dispatch(event)

    assert not result.skipped


# --- レコード特定・差分更新 -------------------------------------------------------------


def test_dispatch_skips_unknown_record(store: SQLiteIdMappingStore) -> None:
    dispatcher = Dispatcher(store, _all_targets())
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="no-such-id",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "unknown_record"


def test_dispatch_skips_stale_event(store: SQLiteIdMappingStore, mapping: IdMapping) -> None:
    dispatcher = Dispatcher(store, _all_targets())
    stale_time = mapping.last_synced_at - timedelta(hours=1)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=stale_time,
        properties={"取引先名": "古い名称"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "stale_event"


def test_dispatch_updates_last_synced_at(store: SQLiteIdMappingStore, mapping: IdMapping) -> None:
    dispatcher = Dispatcher(store, _all_targets())
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    dispatcher.dispatch(event)

    assert store.get("CLI-001").last_synced_at == NOW


# --- Self-Exclusion / Notion発イベントの単純伝播 -----------------------------------------


def test_notion_source_propagates_to_other_tools_and_excludes_itself(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    targets = _all_targets()
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = dispatcher.dispatch(event)

    assert not result.skipped
    assert targets[Tool.NOTION].upsert_calls == []  # Self-Exclusion
    assert targets[Tool.KINTONE].upsert_calls == [("1001", {"取引先名": "新名称"})]
    assert targets[Tool.ZOHO].upsert_calls == [("zoho-1", {"取引先名": "新名称"})]
    assert targets[Tool.SPREADSHEET].upsert_calls == [("5", {"取引先名": "新名称"})]

    prop_result = result.properties[0]
    assert prop_result.resolution is None
    assert prop_result.written_tools == frozenset({Tool.KINTONE, Tool.ZOHO, Tool.SPREADSHEET})


def test_kintone_source_excludes_kintone_from_writes(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "同じ名前", "updated_at": NOW - timedelta(hours=2)}}
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "同じ名前"},
    )

    dispatcher.dispatch(event)

    assert targets[Tool.KINTONE].upsert_calls == []  # 送信元には書き戻さない


# --- sync_scopeによるツール絞り込み ------------------------------------------------------


def test_notion_source_respects_spreadsheet_only_sync_scope(
    store: SQLiteIdMappingStore, mapping: IdMapping, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SPREADSHEET_ONLYスコープの伝播（Notion発の変更がスプレッドシートにのみ伝わり
    kintone/Zohoには伝播しないこと）を検証する。

    08_外部データ連携（国交省/観光庁オープンデータの自動補記）が保留中で、実データスキーマ上
    SPREADSHEET_ONLYスコープを持つプロパティが現状1つも存在しないため、テスト用の
    DatabaseSchemaをその場で組み立て、実際のALL_SCHEMASに依存しない自己完結したテストにする。
    """
    test_schema = DatabaseSchema(
        key="client_master",
        display_name="取引先マスタ（テスト用）",
        id_prefix="CLI",
        kintone_key="取引先マスタ",
        zoho_key="取引先",
        zoho_api_module="Accounts",
        spreadsheet_sheet_name="取引先マスタ",
        properties=(
            PropertyDefinition(
                name="取引先名",
                property_type=PropertyType.TITLE,
                requirement=RequirementLevel.REQUIRED,
                sync_scope=SyncScope.ALL_TOOLS,
            ),
            PropertyDefinition(
                name="エリア属性データ",
                property_type=PropertyType.JSON_TEXT,
                requirement=RequirementLevel.OPTIONAL,
                sync_scope=SyncScope.SPREADSHEET_ONLY,
            ),
        ),
    )
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: test_schema)

    targets = _all_targets()
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"エリア属性データ": '{"pref": "石川県"}'},
    )

    result = dispatcher.dispatch(event)

    assert targets[Tool.SPREADSHEET].upsert_calls == [
        ("5", {"エリア属性データ": '{"pref": "石川県"}'})
    ]
    assert targets[Tool.KINTONE].upsert_calls == []
    assert targets[Tool.ZOHO].upsert_calls == []
    assert result.properties[0].written_tools == frozenset({Tool.SPREADSHEET})


# --- コンフリクト判定を経由する書き込み（非Notion発イベント） ----------------------------


def test_conflict_no_op_when_values_already_match(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "同じ名前", "updated_at": NOW - timedelta(hours=2)}}
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "同じ名前"},
    )

    result = dispatcher.dispatch(event)

    assert result.properties[0].resolution.action == ResolutionAction.NO_OP
    assert notion.upsert_calls == []
    assert targets[Tool.ZOHO].upsert_calls == []
    assert targets[Tool.SPREADSHEET].upsert_calls == []


def test_conflict_propagates_value_from_source_to_notion_and_other_tools(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "", "updated_at": NOW - timedelta(hours=5)}}
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新規登録名"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.PROPAGATE_VALUE
    assert notion.upsert_calls == [("CLI-001", {"取引先名": "新規登録名"})]
    assert targets[Tool.ZOHO].upsert_calls == [("zoho-1", {"取引先名": "新規登録名"})]
    assert targets[Tool.SPREADSHEET].upsert_calls == [("5", {"取引先名": "新規登録名"})]
    assert targets[Tool.KINTONE].upsert_calls == []  # 送信元は既に正しい値を持つ


def test_conflict_propagates_delete_from_source_to_notion_and_other_tools(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "旧名称", "updated_at": NOW - timedelta(hours=5)}}
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": ""},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.PROPAGATE_DELETE
    assert notion.upsert_calls == [("CLI-001", {"取引先名": None})]
    assert targets[Tool.KINTONE].upsert_calls == []  # 送信元(既に空欄)には書き戻さない


def test_conflict_notion_override_corrects_all_other_tools_including_source(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "Notion側の名前", "updated_at": NOW - timedelta(hours=1)}}
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "kintone側の名前"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.NOTION_OVERRIDE
    assert prop.resolution.resolved_value == "Notion側の名前"
    assert len(prop.resolution.rejected) == 1
    assert prop.resolution.rejected[0].rejected_value == "kintone側の名前"
    assert notion.upsert_calls == []  # Notionは既に正しい値を保持
    assert targets[Tool.KINTONE].upsert_calls == [("1001", {"取引先名": "Notion側の名前"})]
    assert targets[Tool.ZOHO].upsert_calls == [("zoho-1", {"取引先名": "Notion側の名前"})]
    assert targets[Tool.SPREADSHEET].upsert_calls == [("5", {"取引先名": "Notion側の名前"})]


# --- BLOCKER1: Notionレコード取得失敗時のデータ消失防止 ------------------------------------


def test_notion_unavailable_falls_back_to_simple_propagate_without_data_loss(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """Notion側レコードが取得できない（未取得・削除済み・API障害等）場合、これを「空欄」と
    誤判定して他ツールへNoneを伝播してはいけない（ソース側の値をそのまま保持・補完する）。
    """
    targets = _all_targets()  # 既定のFakeSyncTarget(NOTION)はget_record()で常にNoneを返す
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新規登録名"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution is None  # コンフリクト判定自体をスキップする
    assert targets[Tool.NOTION].upsert_calls == [("CLI-001", {"取引先名": "新規登録名"})]
    assert targets[Tool.ZOHO].upsert_calls == [("zoho-1", {"取引先名": "新規登録名"})]
    assert targets[Tool.SPREADSHEET].upsert_calls == [("5", {"取引先名": "新規登録名"})]
    assert targets[Tool.KINTONE].upsert_calls == []  # 送信元には書き戻さない


# --- BLOCKER3: Notion・送信元の2者間比較に限定しないコンフリクト検知 -----------------------


def test_conflict_considers_all_sync_scope_tools_not_only_notion_and_source(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """送信元とNotionの値が一致していても、第三のツール（Zoho）が異なる値を持つ場合は
    コンフリクトとして検知できることを確認する（2者間比較のみだとNO_OPに埋もれてしまう）。
    """
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "A", "updated_at": NOW - timedelta(hours=2)}}
    )
    zoho = FakeSyncTarget(
        Tool.ZOHO, records={"zoho-1": {"取引先名": "B", "updated_at": NOW - timedelta(hours=3)}}
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = zoho
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "A"},  # Notionの値と一致（2者間比較だけならNO_OPになってしまう）
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.NOTION_OVERRIDE
    assert prop.resolution.resolved_value == "A"
    assert {r.rejected_tool for r in prop.resolution.rejected} == {Tool.ZOHO}
    assert targets[Tool.ZOHO].upsert_calls == [("zoho-1", {"取引先名": "A"})]
    assert targets[Tool.KINTONE].upsert_calls == []  # 送信元は既にNotionと同じ値


# --- BLOCKER2: データ退避（同期ログ）・Slackアラート通知 ---------------------------------


def test_conflict_rejected_data_logged_to_spreadsheet_regardless_of_importance(
    store: SQLiteIdMappingStore,
) -> None:
    """NOTION_OVERRIDE時の却下データは、重要項目でなくても必ずスプレッドシート
    「同期ログ」タブへ退避されることを確認する。"""
    m = IdMapping(
        notion_key="MSA-PJ-001",
        db_key="project",
        kintone_id="2001",
        zoho_id="zoho-9",
        spreadsheet_row=8,
        last_synced_at=NOW - timedelta(days=1),
    )
    store.upsert(m)
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={"MSA-PJ-001": {"案件名": "Notion側案件名", "updated_at": NOW - timedelta(hours=1)}},
    )
    spreadsheet_client = FakeSpreadsheetClient()
    targets: dict[Tool, Any] = {
        Tool.NOTION: notion,
        Tool.KINTONE: FakeSyncTarget(Tool.KINTONE),
        Tool.ZOHO: FakeSyncTarget(Tool.ZOHO),
        Tool.SPREADSHEET: SpreadsheetSyncTarget(spreadsheet_client, "案件管理"),
    }
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="project",
        external_id="2001",
        occurred_at=NOW,
        properties={"案件名": "kintone側案件名"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.NOTION_OVERRIDE
    assert not prop.resolution.notify_slack  # 「案件名」は重要項目リストに無い

    logged_rows = spreadsheet_client.rows.get(SYNC_LOG_SHEET_NAME, {})
    assert len(logged_rows) == 1
    logged = next(iter(logged_rows.values()))
    assert logged["対象ID"] == "MSA-PJ-001"
    assert logged["項目名"] == "案件名"
    assert logged["採用値"] == "Notion側案件名"
    assert logged["却下値"] == "kintone側案件名"
    assert logged["却下元ツール"] == "kintone"


# --- スキーマ未定義プロパティのスキップ（Dispatcher堅牢性向上） --------------------------


def test_dispatch_skips_unknown_property_while_writing_known_property(
    store: SQLiteIdMappingStore, mapping: IdMapping, caplog: pytest.LogCaptureFixture
) -> None:
    """スキーマ未定義のプロパティと定義済みのプロパティが1つのイベントに混在する場合、
    未定義プロパティはwritten_toolsに一切現れずupsert_recordも呼ばれない一方、
    定義済みプロパティは通常通り書き込まれresult.propertiesに含まれることを確認する。
    """
    targets = _all_targets()
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称", "存在しない項目": "何かの値"},
    )

    with caplog.at_level("WARNING"):
        result = dispatcher.dispatch(event)

    assert not result.skipped
    # 未定義プロパティはresult.propertiesにも現れず、書き込みも一切発生しない。
    assert len(result.properties) == 1
    prop_result = result.properties[0]
    assert prop_result.property_name == "取引先名"
    assert prop_result.written_tools == frozenset({Tool.KINTONE, Tool.ZOHO, Tool.SPREADSHEET})

    for target in targets.values():
        for _external_id, properties in target.upsert_calls:
            assert "存在しない項目" not in properties

    # 定義済みプロパティは通常通り書き込まれる。
    assert targets[Tool.KINTONE].upsert_calls == [("1001", {"取引先名": "新名称"})]
    assert targets[Tool.ZOHO].upsert_calls == [("zoho-1", {"取引先名": "新名称"})]
    assert targets[Tool.SPREADSHEET].upsert_calls == [("5", {"取引先名": "新名称"})]

    assert any(
        "存在しない項目" in record.getMessage() for record in caplog.records
    )


def test_dispatch_all_properties_unknown_yields_empty_result_without_error(
    store: SQLiteIdMappingStore, mapping: IdMapping, caplog: pytest.LogCaptureFixture
) -> None:
    """境界ケース: propertiesが全て未定義の場合、result.properties == ()（空タプル）となり
    例外が発生しないことを確認する。"""
    targets = _all_targets()
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"存在しない項目1": "a", "存在しない項目2": "b"},
    )

    with caplog.at_level("WARNING"):
        result = dispatcher.dispatch(event)

    assert not result.skipped
    assert result.properties == ()
    for target in targets.values():
        assert target.upsert_calls == []
    assert any("存在しない項目1" in record.getMessage() for record in caplog.records)
    assert any("存在しない項目2" in record.getMessage() for record in caplog.records)


def test_conflict_notifies_slack_for_important_property(store: SQLiteIdMappingStore) -> None:
    """重要項目（config/conflict_alert_properties.json）のコンフリクト自動解決時は
    SlackNotifierへ採用データ・却下データが通知されることを確認する。"""
    m = IdMapping(
        notion_key="MSA-PJ-002",
        db_key="project",
        kintone_id="2002",
        zoho_id="zoho-10",
        spreadsheet_row=9,
        last_synced_at=NOW - timedelta(days=1),
    )
    store.upsert(m)
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={"MSA-PJ-002": {"営業ステータス": "商談中(B)", "updated_at": NOW - timedelta(hours=1)}},
    )
    targets: dict[Tool, Any] = {
        Tool.NOTION: notion,
        Tool.KINTONE: FakeSyncTarget(Tool.KINTONE),
        Tool.ZOHO: FakeSyncTarget(Tool.ZOHO),
        Tool.SPREADSHEET: FakeSyncTarget(Tool.SPREADSHEET),
    }
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="project",
        external_id="2002",
        occurred_at=NOW,
        properties={"営業ステータス": "失注"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.notify_slack is True
    assert len(notifier.notified) == 1
    assert notifier.notified[0].rejected_value == "失注"
    assert notifier.notified[0].adopted_value == "商談中(B)"


# --- skipped_tools伝播（obasan-quality/shirokuma-secレビュー: 「同期スキップが成功として
# 見える」問題の修正） -------------------------------------------------------------------
#
# SyncTarget.upsert_record()の契約上、ツール側の都合で実際には書き込まれなかった場合は
# Noneが返る（例: `_MultiDb*SyncTarget`が外部IDからdb_keyを解決できなかった場合）。
# written_tools/skipped_toolsがこれを正しく反映することを検証する。


def test_notion_source_propagation_reports_skipped_tool_when_target_declines_write(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """Notion発の単純伝播で、あるツールのupsert_record()がNoneを返した（実際には
    書き込めなかった）場合、そのツールはwritten_toolsではなくskipped_toolsに現れること。"""
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(Tool.KINTONE, always_skip=True)
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    # 書き込みは試みられる（upsert_callsには記録される）が、成功はしていない。
    assert targets[Tool.KINTONE].upsert_calls == [("1001", {"取引先名": "新名称"})]
    assert prop.written_tools == frozenset({Tool.ZOHO, Tool.SPREADSHEET})
    assert prop.skipped_tools == frozenset({Tool.KINTONE})
    assert result.has_partial_skips is True
    # dispatch全体としては処理された（skipped=Falseのまま）。プロパティ単位の部分的な
    # スキップと、dispatch全体のskipped（own_system_event/unknown_record/stale_event用）は
    # 別軸であることを明示する。
    assert result.skipped is False


def test_notion_unavailable_fallback_reports_skipped_tool_when_target_declines_write(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """BLOCKER1のNotion取得不可フォールバック経路でも、skipped_toolsが正しく反映されること。"""
    targets = _all_targets()  # NOTIONのget_record()は常にNoneを返す（BLOCKER1経路に入る）
    targets[Tool.ZOHO] = FakeSyncTarget(Tool.ZOHO, always_skip=True)
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新規登録名"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.written_tools == frozenset({Tool.NOTION, Tool.SPREADSHEET})
    assert prop.skipped_tools == frozenset({Tool.ZOHO})


def test_conflict_resolution_reports_skipped_tool_when_target_declines_write(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """コンフリクト解決経由の書き込みでも、skipped_toolsが正しく反映されること。"""
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "", "updated_at": NOW - timedelta(hours=5)}}
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.SPREADSHEET] = FakeSyncTarget(Tool.SPREADSHEET, always_skip=True)
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新規登録名"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.PROPAGATE_VALUE
    assert prop.written_tools == frozenset({Tool.NOTION, Tool.ZOHO})
    assert prop.skipped_tools == frozenset({Tool.SPREADSHEET})
    assert result.has_partial_skips is True


def test_has_partial_skips_is_false_when_all_intended_writes_succeed(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    dispatcher = Dispatcher(store, _all_targets())
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = dispatcher.dispatch(event)

    assert result.has_partial_skips is False


def test_write_skipped_because_target_not_configured_at_all_is_also_reported(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """targetsに当該ツールのSyncTargetが一切登録されていない場合も、書き込み対象として
    意図はされていた（sync_scope上は対象）ため、written_toolsではなくskipped_toolsに
    現れること（未接続ツールへの書き込みが暗黙に「成功扱い」にならないようにする）。"""
    targets = _all_targets()
    del targets[Tool.ZOHO]
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.written_tools == frozenset({Tool.KINTONE, Tool.SPREADSHEET})
    assert prop.skipped_tools == frozenset({Tool.ZOHO})
