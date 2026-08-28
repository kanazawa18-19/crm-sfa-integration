"""dispatcherのSelf-Exclusion・無限ループ防止・sync_scope判定・コンフリクト経由の書き込みを検証する。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import requests

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
    Tool,
)
from src.sync_engine.clients.kintone_client import KintoneApiError
from src.sync_engine.clients.notion_client import NOTION_LAST_EDITED_TIME_KEY, NotionApiError
from src.sync_engine.clients.zoho_client import ZohoApiError
from src.sync_engine.conflict_resolver import RejectedData, ResolutionAction
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import DuplicateExternalIdError, IdMapping, SQLiteIdMappingStore
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

    `get_record_raises`は既定（`get_record_raises_after_calls=0`）では呼び出し回数に
    関係なく毎回例外を投げるが、`get_record_raises_after_calls`にN（>0）を指定すると
    「(このインスタンスへの)N回目までの呼び出しは成功し、N+1回目以降の呼び出しから例外を
    投げる」動作に切り替えられる（WARN3対応、2026-08-28: 「1つ目のプロパティは
    get_record成功→書き込み成功、2つ目のプロパティでget_recordが失敗する」という、
    複数プロパティイベントでの部分書き込みシナリオをテストで再現するために追加）。
    """

    def __init__(
        self,
        tool: Tool,
        records: dict[str, dict[str, Any]] | None = None,
        *,
        always_skip: bool = False,
        delete_raises: bool = False,
        upsert_raises: Exception | None = None,
        get_record_raises: Exception | None = None,
        get_record_raises_after_calls: int = 0,
    ) -> None:
        self.tool = tool
        self._records = records or {}
        self._always_skip = always_skip
        self._delete_raises = delete_raises
        self._upsert_raises = upsert_raises
        self._get_record_raises = get_record_raises
        self._get_record_raises_after_calls = get_record_raises_after_calls
        self.upsert_calls: list[tuple[str | None, dict[str, Any]]] = []
        self.delete_calls: list[str] = []
        self.get_record_calls: list[str] = []

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        self.get_record_calls.append(external_id)
        if self._get_record_raises is not None and (
            len(self.get_record_calls) > self._get_record_raises_after_calls
        ):
            raise self._get_record_raises
        return self._records.get(external_id)

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str | None:
        self.upsert_calls.append((external_id, dict(properties)))
        if self._upsert_raises is not None:
            raise self._upsert_raises
        if self._always_skip:
            return None
        return external_id or "new-id"

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        self.delete_calls.append(external_id)
        if self._delete_raises:
            raise RuntimeError("archive failed")


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


class _FlakyIdMappingStore:
    """`SQLiteIdMappingStore`をラップし、`upsert()`を指定回数だけ失敗させるテスト用スタブ
    （BLOCKER1対応、2026-08-25: 新規レコード作成時のIdMapping登録リトライ・補償アクションの
    検証用。`upsert()`以外は内側のストアへそのまま委譲する）。
    """

    def __init__(
        self, inner: SQLiteIdMappingStore, *, fail_times: int, exc: Exception | None = None
    ) -> None:
        self._inner = inner
        self._fail_times = fail_times
        self._exc = exc or RuntimeError("transient id mapping store failure")
        self.upsert_attempts = 0

    def get(self, notion_key: str) -> IdMapping | None:
        return self._inner.get(notion_key)

    def upsert(self, mapping: IdMapping, **kwargs: Any) -> None:
        self.upsert_attempts += 1
        if self.upsert_attempts <= self._fail_times:
            raise self._exc
        self._inner.upsert(mapping, **kwargs)

    def delete(self, notion_key: str) -> None:
        self._inner.delete(notion_key)

    def find_by_external_id(self, tool: Tool, external_id: str, *, db_key: str) -> IdMapping | None:
        return self._inner.find_by_external_id(tool, external_id, db_key=db_key)

    def update_last_synced_at(self, notion_key: str, synced_at: datetime) -> None:
        self._inner.update_last_synced_at(notion_key, synced_at)

    def list_by_db(self, db_key: str) -> list[IdMapping]:
        return self._inner.list_by_db(db_key)


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
    """SlackNotifier の呼び出し内容を記録するテスト用スタブ。"""

    def __init__(self) -> None:
        self.notified: list[RejectedData] = []
        self.new_record_created_calls: list[dict[str, Any]] = []
        self.new_record_issue_calls: list[dict[str, Any]] = []
        self.update_skipped_calls: list[dict[str, Any]] = []

    def notify_conflict(self, rejected: RejectedData) -> None:
        self.notified.append(rejected)

    def notify_new_record_created(
        self, *, db_key: str, source_tool: Tool, external_id: str, notion_page_id: str
    ) -> None:
        self.new_record_created_calls.append(
            {
                "db_key": db_key,
                "source_tool": source_tool,
                "external_id": external_id,
                "notion_page_id": notion_page_id,
            }
        )

    def notify_new_record_issue(
        self,
        *,
        db_key: str,
        source_tool: Tool,
        external_id: str,
        reason: str,
        detail: str,
        notion_page_id: str | None = None,
    ) -> None:
        self.new_record_issue_calls.append(
            {
                "db_key": db_key,
                "source_tool": source_tool,
                "external_id": external_id,
                "reason": reason,
                "detail": detail,
                "notion_page_id": notion_page_id,
            }
        )

    def notify_update_skipped(
        self,
        *,
        db_key: str,
        source_tool: Tool,
        external_id: str,
        reason: str,
        detail: str,
    ) -> None:
        self.update_skipped_calls.append(
            {
                "db_key": db_key,
                "source_tool": source_tool,
                "external_id": external_id,
                "reason": reason,
                "detail": detail,
            }
        )


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


# --- 新規レコード作成（AUTO_CREATE_NEW_RECORDS_ENABLED、2026-08-25、Round2） ------------------


def _kintone_client_master_record() -> dict[str, Any]:
    # KINTONE_FIELD_TRANSFORMS["client_master"]の実フィールドコード。「顧客名」→「取引先名」
    # （REQUIRED・title）のみで必須プロパティを満たせる（client_masterの必須項目は
    # 「取引先名」のみ、tests/sync_engine/test_new_record_builder.pyと同じ前提）。
    return {"顧客名": "新規商事", "顧客種別": "ホテル・旅館", "TEL": "03-1234-5678"}


def _zoho_project_record() -> dict[str, Any]:
    # ZOHO_LABEL_FIELD_MAPPINGS["project"]の実api_name（config/zoho_field_mapping.json検証済み、
    # tests/sync_engine/webhook_handlers/test_zoho_webhook.pyと同じ実api_name）。projectの必須
    # プロパティ「案件名」「営業ステータス」の両方を満たせる。
    return {"Deal_Name": "新規案件", "Stage": "商談中(B)"}


def test_dispatch_skips_unknown_record_when_flag_unset_even_though_source_data_would_suffice(
    store: SQLiteIdMappingStore,
) -> None:
    """既存動作の完全維持を確認する回帰テスト: AUTO_CREATE_NEW_RECORDS_ENABLEDが未設定の場合、
    ソース側に必須プロパティを満たす十分なデータがあっても新規作成は一切行わず、従来通り
    unknown_recordとしてスキップすること（ソースレコードの取得すら行わない）。"""
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={"取引先名": "新規商事"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "unknown_record"
    assert targets[Tool.KINTONE].get_record_calls == []
    assert targets[Tool.NOTION].upsert_calls == []
    assert store.find_by_external_id(Tool.KINTONE, "kintone-new-1", db_key="client_master") is None


def test_dispatch_creates_new_notion_page_from_kintone_record_when_enabled(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},  # Webhookの変更差分そのものは新規作成では使わない（全体データを再取得）。
    )

    result = dispatcher.dispatch(event)

    assert not result.skipped
    assert targets[Tool.KINTONE].get_record_calls == ["kintone-new-1"]
    assert len(targets[Tool.NOTION].upsert_calls) == 1
    external_id, properties = targets[Tool.NOTION].upsert_calls[0]
    assert external_id is None
    assert properties == {
        "取引先名": "新規商事",
        "顧客種別": "ホテル・旅館",
        "TEL": "03-1234-5678",
    }
    new_mapping = store.find_by_external_id(Tool.KINTONE, "kintone-new-1", db_key="client_master")
    assert new_mapping is not None
    assert new_mapping.notion_key == "new-id"  # FakeSyncTarget.upsert_recordの既定戻り値
    assert new_mapping.last_synced_at == NOW
    # obasan-quality/shirokuma-secレビューWARN対応（2026-08-25）: 新規ページ作成成功もSlackへ通知する。
    assert notifier.new_record_created_calls == [
        {
            "db_key": "client_master",
            "source_tool": Tool.KINTONE,
            "external_id": "kintone-new-1",
            "notion_page_id": "new-id",
        }
    ]
    assert notifier.new_record_issue_calls == []


def test_dispatch_creates_new_notion_page_from_zoho_record_when_enabled(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.ZOHO] = FakeSyncTarget(Tool.ZOHO, {"zoho-new-1": _zoho_project_record()})
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.ZOHO,
        db_key="project",
        external_id="zoho-new-1",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert not result.skipped
    external_id, properties = targets[Tool.NOTION].upsert_calls[0]
    assert external_id is None
    assert properties == {"案件名": "新規案件", "営業ステータス": "商談中(B)"}
    new_mapping = store.find_by_external_id(Tool.ZOHO, "zoho-new-1", db_key="project")
    assert new_mapping is not None
    assert new_mapping.notion_key == "new-id"


def test_dispatch_skips_new_record_creation_when_source_tool_has_no_target(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    del targets[Tool.ZOHO]
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.ZOHO,
        db_key="project",
        external_id="zoho-new-1",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_source_unavailable"
    assert targets[Tool.NOTION].upsert_calls == []


def test_dispatch_skips_new_record_creation_when_source_record_not_found(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()  # Tool.ZOHOのFakeSyncTargetにレコード登録なし。
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.ZOHO,
        db_key="project",
        external_id="zoho-missing",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_source_not_found"
    assert targets[Tool.NOTION].upsert_calls == []


# --- 元レコード取得が例外を送出するケース（2026-08-27本番障害対応） ------------------------
#
# `_try_create_new_record()`は元々「元レコードが見つからない（get_record()がNoneを返す）」
# 場合しか想定しておらず、「取得そのものが例外を送出する」場合（外部APIエラー・ネットワーク
# 障害）は例外がWebhookハンドラまで伝播し500応答になっていた
# （2026-08-27 15:13 UTC、kintone db_key='client_master'、KintoneApiError「HTTP 400:
# 不正なリクエストです。」で本番発生）。以下は、その場合に例外を伝播させずスキップへ倒し、
# Slack通知が呼ばれることの回帰テスト。


def test_dispatch_skips_new_record_creation_when_source_fetch_raises_api_error(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, get_record_raises=KintoneApiError(400, "不正なリクエストです。")
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-broken",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_source_fetch_failed"
    assert targets[Tool.NOTION].upsert_calls == []
    assert store.find_by_external_id(Tool.KINTONE, "kintone-broken", db_key="client_master") is None
    assert len(notifier.new_record_issue_calls) == 1
    issue_call = notifier.new_record_issue_calls[0]
    assert issue_call["db_key"] == "client_master"
    assert issue_call["source_tool"] is Tool.KINTONE
    assert issue_call["external_id"] == "kintone-broken"
    assert issue_call["reason"] == "source_record_fetch_failed"
    assert notifier.new_record_created_calls == []


def test_dispatch_skips_new_record_creation_when_zoho_source_fetch_raises_network_error(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """Zoho発の未知レコードでも同じ経路が塞がれていること（タイムアウト等のネットワーク
    障害系、`requests.exceptions.RequestException`のケース）。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.ZOHO] = FakeSyncTarget(
        Tool.ZOHO, get_record_raises=requests.exceptions.ConnectionError("connection refused")
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.ZOHO,
        db_key="project",
        external_id="zoho-broken",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_source_fetch_failed"
    assert targets[Tool.NOTION].upsert_calls == []
    assert len(notifier.new_record_issue_calls) == 1
    assert notifier.new_record_issue_calls[0]["reason"] == "source_record_fetch_failed"


def test_dispatch_new_record_creation_lets_programming_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """ApiError/RequestException以外（プログラミングエラーの疑いがある例外）は、意図的に
    握らず従来通り呼び出し元へ伝播すること（握りすぎてバグを隠さないための回帰テスト）。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, get_record_raises=AttributeError("boom")
    )
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-broken",
        occurred_at=NOW,
        properties={},
    )

    with pytest.raises(AttributeError):
        dispatcher.dispatch(event)


def test_dispatch_skips_new_record_creation_when_required_property_missing(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore, caplog: pytest.LogCaptureFixture
) -> None:
    """必須プロパティが欠けている場合、不完全なNotionページを作らずスキップすること
    （例: ⑥アクション履歴DBのtitleプロパティ「商談回数・電話回数・メール回数（何回目）」は
    KINTONE_FIELD_TRANSFORMS["action"]にkintone側の対応フィールドが存在しないため常に導出
    できず、kintone発のアクション新規レコードは必須項目不足で作成されない）。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-action-1": {"comment": "折り返し予定"}}
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="action",
        external_id="kintone-action-1",
        occurred_at=NOW,
        properties={},
    )

    with caplog.at_level("WARNING"):
        result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_missing_required_properties"
    assert targets[Tool.NOTION].upsert_calls == []
    assert store.find_by_external_id(Tool.KINTONE, "kintone-action-1", db_key="action") is None
    assert any("missing" in r.getMessage() for r in caplog.records)
    # obasan-quality/shirokuma-secレビューWARN対応（2026-08-25）: 必須プロパティ不足による
    # スキップもSlackへ通知する。
    assert len(notifier.new_record_issue_calls) == 1
    issue = notifier.new_record_issue_calls[0]
    assert issue["reason"] == "missing_required_properties"
    assert issue["notion_page_id"] is None
    assert notifier.new_record_created_calls == []


def test_dispatch_skips_new_record_creation_when_notion_target_missing(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    del targets[Tool.NOTION]
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_notion_target_unavailable"


def test_dispatch_skips_new_record_creation_when_notion_creation_is_skipped_by_target(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """Notion側の新規ページ作成自体が（本番では`_MultiDbNotionSyncTarget`がdb_key未設定等の
    理由で）スキップされた場合、IdMappingを登録せずスキップとして報告すること。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    targets[Tool.NOTION] = FakeSyncTarget(Tool.NOTION, always_skip=True)
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_creation_failed"
    assert store.find_by_external_id(Tool.KINTONE, "kintone-new-1", db_key="client_master") is None


# --- 重複作成の防止（BLOCKER1対応、2026-08-25） ------------------------------------------------


def test_dispatch_skips_new_record_creation_when_mapping_appears_immediately_before_create(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """Notionページ作成直前の再確認（レース窓縮小）: `_resolve_mapping()`の最初の呼び出しでは
    Noneだったが、その直後（＝並行Webhookが先に作成を完了させた想定）にmappingが見つかった
    場合、重複してNotionページを作成しないこと。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    original_resolve_mapping = dispatcher._resolve_mapping  # noqa: SLF001 (テストのため直接差し替え)
    call_count = 0

    def _resolve_mapping_then_appear(event: SyncEvent) -> IdMapping | None:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return original_resolve_mapping(event)
        # 2回目（create_page直前の再確認）: 並行Webhookが先に作成・登録を終えた状態を模す。
        return IdMapping(
            notion_key="concurrently-created-page",
            db_key="client_master",
            kintone_id="kintone-new-1",
            last_synced_at=NOW,
        )

    monkeypatch.setattr(dispatcher, "_resolve_mapping", _resolve_mapping_then_appear)

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_concurrent_creation_detected"
    assert targets[Tool.NOTION].upsert_calls == []
    assert call_count == 2


def test_dispatch_retries_mapping_registration_and_succeeds_on_transient_failure(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """IdMapping登録が一時的な障害で失敗しても、リトライで最終的に成功すれば通常通り成功
    として扱い、補償アクション（アーカイブ）は発生しないこと。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    monkeypatch.setattr("src.sync_engine.dispatcher.time.sleep", lambda seconds: None)
    flaky_store = _FlakyIdMappingStore(store, fail_times=1)
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(flaky_store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert not result.skipped
    assert flaky_store.upsert_attempts == 2  # 1回目失敗、2回目成功
    assert targets[Tool.NOTION].delete_calls == []  # 最終的に成功したので補償アクション不要
    new_mapping = store.find_by_external_id(Tool.KINTONE, "kintone-new-1", db_key="client_master")
    assert new_mapping is not None
    assert notifier.new_record_created_calls  # 成功として通常通り通知される
    assert notifier.new_record_issue_calls == []


def test_dispatch_archives_orphaned_notion_page_when_mapping_registration_permanently_fails(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore, caplog: pytest.LogCaptureFixture
) -> None:
    """IdMapping登録が数回リトライしても失敗し続けた場合、作成済みのNotionページを
    アーカイブする補償アクションを実行し、孤児ページIDを含むSlackアラートを出すこと。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    monkeypatch.setattr("src.sync_engine.dispatcher.time.sleep", lambda seconds: None)
    flaky_store = _FlakyIdMappingStore(store, fail_times=999)  # 常に失敗
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(flaky_store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    with caplog.at_level("ERROR"):
        result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_mapping_registration_failed"
    # リトライ回数分（初回+2回）だけ試行し、深追いしすぎない。
    assert flaky_store.upsert_attempts == 3
    # 補償アクション: 作成済みのNotionページ（"new-id"）をアーカイブする。
    assert targets[Tool.NOTION].delete_calls == ["new-id"]
    # 実際にはstore.upsert()が一度も成功していないため、マッピングは登録されないまま。
    assert store.find_by_external_id(Tool.KINTONE, "kintone-new-1", db_key="client_master") is None
    assert any("mapping registration failed" in r.getMessage() for r in caplog.records)
    assert len(notifier.new_record_issue_calls) == 1
    issue = notifier.new_record_issue_calls[0]
    assert issue["reason"] == "mapping_registration_failed"
    assert issue["notion_page_id"] == "new-id"
    assert "アーカイブ済み" in issue["detail"]
    assert notifier.new_record_created_calls == []


def test_dispatch_alerts_even_when_orphaned_page_archive_itself_fails(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """補償アクション（アーカイブ）自体にも失敗した場合、サイレントに諦めず、その旨を
    明示したSlackアラートを出すこと。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    monkeypatch.setattr("src.sync_engine.dispatcher.time.sleep", lambda seconds: None)
    flaky_store = _FlakyIdMappingStore(store, fail_times=999)
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    targets[Tool.NOTION] = FakeSyncTarget(Tool.NOTION, delete_raises=True)
    notifier = SpyNotifier()
    dispatcher = Dispatcher(flaky_store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_mapping_registration_failed"
    assert targets[Tool.NOTION].delete_calls == ["new-id"]  # アーカイブは試みられた
    assert len(notifier.new_record_issue_calls) == 1
    issue = notifier.new_record_issue_calls[0]
    assert issue["notion_page_id"] == "new-id"
    assert "アーカイブにも失敗" in issue["detail"]


def test_dispatch_stops_immediately_on_duplicate_external_id_without_retrying(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """obasan-quality/shirokuma-secレビューWARN対応（2026-08-25、最終レビュー）:
    `DuplicateExternalIdError`（真の並行作成による恒久的な失敗）はリトライしても結果が
    変わらないため、待機・リトライせず即座に補償アクション（アーカイブ+アラート）へ進むこと。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "src.sync_engine.dispatcher.time.sleep", lambda seconds: sleep_calls.append(seconds)
    )
    duplicate_error = DuplicateExternalIdError(Tool.KINTONE, "kintone-new-1", "other-page")
    flaky_store = _FlakyIdMappingStore(store, fail_times=999, exc=duplicate_error)
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(flaky_store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_mapping_registration_failed"
    assert flaky_store.upsert_attempts == 1  # リトライしない(1回だけ試して即座に諦める)
    assert sleep_calls == []  # 待機もしない
    assert targets[Tool.NOTION].delete_calls == ["new-id"]  # 補償アクションは通常通り実行される
    assert len(notifier.new_record_issue_calls) == 1
    assert notifier.new_record_issue_calls[0]["reason"] == "mapping_registration_failed"


def test_dispatch_register_mapping_backs_off_between_retries_but_not_after_last_attempt(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """obasan-quality/shirokuma-secレビューWARN対応（2026-08-25、最終レビュー）: 一時的な
    障害によるリトライの間には固定の短い待機を挟み、最終試行後には待機しないこと。"""
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    sleep_calls: list[float] = []
    monkeypatch.setattr(
        "src.sync_engine.dispatcher.time.sleep", lambda seconds: sleep_calls.append(seconds)
    )
    flaky_store = _FlakyIdMappingStore(store, fail_times=999)  # 常に失敗(汎用の一時的エラー)
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    dispatcher = Dispatcher(flaky_store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    dispatcher.dispatch(event)

    # 初回+2回リトライ(計3回試行) → 試行間の待機は2回(3回目の失敗後は待機しない)。
    assert flaky_store.upsert_attempts == 3
    assert len(sleep_calls) == 2
    assert all(seconds > 0 for seconds in sleep_calls)


# --- create_page()自体が例外を送出するケース（最終レビューBLOCKER対応、2026-08-25） -------------
# notion_target.upsert_record()（実体はNotion APIへのPOST）自体がタイムアウト・接続断・5xx等で
# 例外を送出した場合、「ページが実際に作られたか不明」な状態として扱い、Webhookハンドラへ
# 例外を伝播させない(=500でリトライを誘発させない)こと。


def test_dispatch_treats_notion_creation_call_exception_as_unknown_status_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    targets[Tool.NOTION] = FakeSyncTarget(
        Tool.NOTION, upsert_raises=TimeoutError("Notion API response timed out")
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    with caplog.at_level("ERROR"):
        # 例外が呼び出し元まで伝播しない(=Webhookハンドラが500を返さない)ことそのものが
        # このテストの主眼(前回レビュー指摘: この保護が無いと、ここでraiseした例外が
        # webhook_handlers側の広いexcept Exceptionまで伝わり500応答→kintone/Zoho側の
        # リトライで重複ページ作成が再現していた)。
        result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_creation_status_unknown"
    # ページIDが分からない(Notion APIからの応答自体を受け取れていない)ため、アーカイブの
    # 補償アクションは行えない(delete_recordは呼ばれない)。
    assert targets[Tool.NOTION].delete_calls == []
    # IdMappingも当然登録されない。
    assert store.find_by_external_id(Tool.KINTONE, "kintone-new-1", db_key="client_master") is None
    assert any("Notion page creation API call raised" in r.getMessage() for r in caplog.records)
    assert len(notifier.new_record_issue_calls) == 1
    issue = notifier.new_record_issue_calls[0]
    assert issue["reason"] == "notion_creation_status_unknown"
    assert issue["notion_page_id"] is None
    assert "手動でNotion側を確認" in issue["detail"]
    assert notifier.new_record_created_calls == []


def test_dispatch_completes_safely_even_when_slack_notification_itself_fails(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """3回目最終レビューBLOCKER対応（2026-08-25）の統合確認: `_handle_uncertain_notion_page_
    creation()`のような「他の保護ロジックが失敗した後の最終防衛線」内のSlack通知自体が
    失敗しても、`dispatch()`全体が例外を送出せず安全に完了すること。実際の
    `WebhookSlackNotifier`（モックではなく本番実装）を使い、その内部の`requests.post()`が
    例外を投げる状況を再現する（`WebhookSlackNotifier`自体が例外を握りつぶす設計になった
    ことの裏付け。個別呼び出し箇所ごとのtry/exceptには依存しない）。"""
    from src.sync_engine.slack_notifier import WebhookSlackNotifier

    def _raise_from_requests_post(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("slack webhook unavailable")

    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    monkeypatch.setattr(
        "src.sync_engine.slack_notifier.requests.post", _raise_from_requests_post
    )
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {"kintone-new-1": _kintone_client_master_record()}
    )
    targets[Tool.NOTION] = FakeSyncTarget(
        Tool.NOTION, upsert_raises=TimeoutError("Notion API response timed out")
    )
    real_notifier = WebhookSlackNotifier("https://hooks.slack.com/services/xxx")
    dispatcher = Dispatcher(store, targets, slack_notifier=real_notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="kintone-new-1",
        occurred_at=NOW,
        properties={},
    )

    # dispatch()が例外を送出しない(=Webhookハンドラが500を返さない)ことそのものが主眼。
    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "new_record_creation_status_unknown"


def test_dispatch_new_record_creation_uses_new_record_builder_and_propagates_relation_value(
    monkeypatch: pytest.MonkeyPatch, store: SQLiteIdMappingStore
) -> None:
    """`Dispatcher`が新規レコード作成時に`build_notion_properties_for_new_record`
    （取引先マスターリレーション解決を含むプロパティ組み立て、`new_record_builder.py`参照）を
    正しい引数で呼び出し、その戻り値（解決済みリレーションを含む）をそのままNotionページ作成に
    使うことを確認する（個々のリレーション解決ロジック自体は
    tests/sync_engine/test_new_record_builder.pyで別途検証済みのため、ここでは統合の配線のみ
    確認する）。"""
    import src.sync_engine.dispatcher as dispatcher_module

    monkeypatch.setenv("AUTO_CREATE_NEW_RECORDS_ENABLED", "true")
    captured: dict[str, Any] = {}

    def _fake_builder(
        *, source_tool: Tool, db_key: str, external_id: str, raw_record: dict[str, Any]
    ) -> dict[str, Any]:
        captured.update(
            source_tool=source_tool, db_key=db_key, external_id=external_id, raw_record=raw_record
        )
        return {
            "商談回数・電話回数・メール回数（何回目）": "【電話】4回目",
            "アクション種別": "テレアポ",
            "👨‍👩‍👧‍👦 取引先マスター": "notion-client-page-1",
        }

    monkeypatch.setattr(
        dispatcher_module, "build_notion_properties_for_new_record", _fake_builder
    )
    raw_zoho_record = {"Name": "【電話】4回目", "field6": "テスト商事"}
    targets = _all_targets()
    targets[Tool.ZOHO] = FakeSyncTarget(Tool.ZOHO, {"zoho-action-1": raw_zoho_record})
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.ZOHO,
        db_key="action",
        external_id="zoho-action-1",
        occurred_at=NOW,
        properties={},
    )

    result = dispatcher.dispatch(event)

    assert not result.skipped
    assert captured == {
        "source_tool": Tool.ZOHO,
        "db_key": "action",
        "external_id": "zoho-action-1",
        "raw_record": raw_zoho_record,
    }
    external_id, properties = targets[Tool.NOTION].upsert_calls[0]
    assert external_id is None
    assert properties["👨‍👩‍👧‍👦 取引先マスター"] == "notion-client-page-1"


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


def test_dispatch_does_not_confuse_records_with_colliding_external_id_across_db_keys(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """回帰テスト（2026-08-14、shirokuma-secレビューBLOCKER対応）: kintoneのレコード番号は
    アプリ（db_key）単位で独立採番されているため、別db_key（ここではaction）に同じ
    kintone_id="1001"を持つレコードが存在しても、正しいdb_keyのマッピングだけが解決される
    こと。実際にkintone→Notion方向のWebhookを有効化した際、これが原因でアクション管理
    アプリのイベントが取引先マスターDBのNotionページを誤って解決する事故が発生した。
    """
    colliding_mapping = IdMapping(
        notion_key="ACT-001",
        db_key="action",
        kintone_id="1001",  # mappingフィクスチャ（client_master）と同じ値、別db_key。
        last_synced_at=NOW - timedelta(days=1),
    )
    store.upsert(colliding_mapping)
    dispatcher = Dispatcher(store, _all_targets())
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={},
    )

    dispatcher.dispatch(event)

    # client_master側のマッピングだけが更新され、action側は無関係のまま。
    assert store.get("CLI-001").last_synced_at == NOW
    assert store.get("ACT-001").last_synced_at == NOW - timedelta(days=1)


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
        Tool.NOTION, records={"CLI-001": {"取引先名": "同じ名前", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=2)}}
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
        Tool.NOTION, records={"CLI-001": {"取引先名": "同じ名前", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=2)}}
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
        Tool.NOTION, records={"CLI-001": {"取引先名": "", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=5)}}
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
        Tool.NOTION, records={"CLI-001": {"取引先名": "旧名称", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=5)}}
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


def test_conflict_source_wins_when_more_recent_than_notions_stale_value(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """2026-08本番障害の再現ケース: Notion側の値が古く（この例ではNOW-1h時点の値のまま）、
    送信元ツールの編集がそれより新しい場合、Notionが無条件に勝つのではなく、より新しい
    送信元側の値が採用されNotionへ書き戻される（最新編集優先ルール）。

    以前はここでNOTION_OVERRIDEが無条件に発生し、Notionの古い値でkintone側の新しい
    編集を強制的に上書きしてしまっていた（実際のZohoステージ変更が失われた本番障害と
    同じ構造のバグ）。
    """
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "Notion側の名前", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=1)}}
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,  # Notion側のupdated_at（NOW-1h）より新しい
        properties={"取引先名": "kintone側の名前"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.PROPAGATE_VALUE
    assert prop.resolution.resolved_value == "kintone側の名前"
    assert len(prop.resolution.rejected) == 1
    assert prop.resolution.rejected[0].rejected_value == "Notion側の名前"
    assert prop.resolution.rejected[0].rejected_tool == Tool.NOTION
    assert notion.upsert_calls == [("CLI-001", {"取引先名": "kintone側の名前"})]  # Notionを是正
    assert targets[Tool.KINTONE].upsert_calls == []  # 送信元は既に正しい値を保持
    assert targets[Tool.ZOHO].upsert_calls == [("zoho-1", {"取引先名": "kintone側の名前"})]
    assert targets[Tool.SPREADSHEET].upsert_calls == [("5", {"取引先名": "kintone側の名前"})]
    # Tool.NOTIONが書き込み対象・成功対象の両方に含まれること（本番障害では
    # written_tools=['zoho'], skipped_tools=['kintone','spreadsheet']で
    # Notionがどちらにも現れず、Notionへの反映漏れに気づけなかった）。
    assert Tool.NOTION in prop.written_tools
    assert Tool.NOTION not in prop.skipped_tools


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


# --- 更新パスの現在値取得が例外を送出するケース（2026-08-27/28本番障害対応の残存リスク決着） ---
#
# `_try_create_new_record()`（新規作成経路）とは別の、`dispatch()`本体（既に`IdMapping`が
# 存在するレコードへの通常の更新イベント）に残っていた未保護の`get_record()`2箇所を塞ぐ。
# 「取得に失敗したら、この同期イベントの書き込みを中止（スキップ）してSlack通知を出す」方針
# （`docs/relation_sync_activation_note.md`参照）。部分的に取得できた値で更新を続けたり、
# 取得できなかったツールを無視して進めたりしないこと（＝書き込みが一切呼ばれないこと）が要点。


def test_dispatch_update_aborts_without_writes_when_notion_current_value_fetch_raises_api_error(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    targets = _all_targets()
    targets[Tool.NOTION] = FakeSyncTarget(
        Tool.NOTION, get_record_raises=NotionApiError(500, "internal error")
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "update_notion_value_fetch_failed"
    assert targets[Tool.NOTION].upsert_calls == []
    assert targets[Tool.KINTONE].upsert_calls == []
    assert targets[Tool.ZOHO].upsert_calls == []
    assert targets[Tool.SPREADSHEET].upsert_calls == []
    # last_synced_atは更新されない（次に届く新しいイベントで再度処理されるようにするため）。
    assert store.get("CLI-001").last_synced_at == mapping.last_synced_at
    assert len(notifier.update_skipped_calls) == 1
    call = notifier.update_skipped_calls[0]
    assert call["db_key"] == "client_master"
    assert call["source_tool"] is Tool.KINTONE
    assert call["external_id"] == "1001"
    assert call["reason"] == "update_notion_value_fetch_failed"


def test_dispatch_update_aborts_without_writes_when_notion_current_value_fetch_raises_network_error(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    targets = _all_targets()
    targets[Tool.NOTION] = FakeSyncTarget(
        Tool.NOTION, get_record_raises=requests.exceptions.ConnectionError("connection refused")
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "update_notion_value_fetch_failed"
    assert targets[Tool.NOTION].upsert_calls == []
    assert targets[Tool.KINTONE].upsert_calls == []
    assert targets[Tool.ZOHO].upsert_calls == []
    assert targets[Tool.SPREADSHEET].upsert_calls == []
    assert len(notifier.update_skipped_calls) == 1
    assert notifier.update_skipped_calls[0]["reason"] == "update_notion_value_fetch_failed"


def test_dispatch_update_lets_programming_errors_propagate_from_notion_current_value_fetch(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """ApiError/RequestException以外は意図的に握らず従来どおり伝播すること（回帰テスト）。"""
    targets = _all_targets()
    targets[Tool.NOTION] = FakeSyncTarget(Tool.NOTION, get_record_raises=AttributeError("boom"))
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新名称"},
    )

    with pytest.raises(AttributeError):
        dispatcher.dispatch(event)


def test_dispatch_update_aborts_without_writes_when_other_tool_current_value_fetch_raises_api_error(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={"CLI-001": {"取引先名": "Notion側の名前", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=2)}},
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = FakeSyncTarget(
        Tool.ZOHO, get_record_raises=ZohoApiError(500, "internal error")
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "kintone側の名前"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "update_target_value_fetch_failed"
    assert notion.upsert_calls == []
    assert targets[Tool.KINTONE].upsert_calls == []
    assert targets[Tool.ZOHO].upsert_calls == []
    assert targets[Tool.SPREADSHEET].upsert_calls == []
    assert store.get("CLI-001").last_synced_at == mapping.last_synced_at
    assert len(notifier.update_skipped_calls) == 1
    call = notifier.update_skipped_calls[0]
    assert call["db_key"] == "client_master"
    assert call["source_tool"] is Tool.KINTONE
    assert call["external_id"] == "1001"
    assert call["reason"] == "update_target_value_fetch_failed"


def test_dispatch_update_aborts_without_writes_when_other_tool_current_value_fetch_raises_network_error(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={"CLI-001": {"取引先名": "Notion側の名前", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=2)}},
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.SPREADSHEET] = FakeSyncTarget(
        Tool.SPREADSHEET, get_record_raises=requests.exceptions.Timeout("timed out")
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "kintone側の名前"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "update_target_value_fetch_failed"
    assert notion.upsert_calls == []
    assert targets[Tool.KINTONE].upsert_calls == []
    assert targets[Tool.ZOHO].upsert_calls == []
    assert targets[Tool.SPREADSHEET].upsert_calls == []
    assert len(notifier.update_skipped_calls) == 1
    assert notifier.update_skipped_calls[0]["reason"] == "update_target_value_fetch_failed"


def test_dispatch_update_lets_programming_errors_propagate_from_other_tool_current_value_fetch(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={"CLI-001": {"取引先名": "Notion側の名前", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=2)}},
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = FakeSyncTarget(Tool.ZOHO, get_record_raises=AttributeError("boom"))
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "kintone側の名前"},
    )

    with pytest.raises(AttributeError):
        dispatcher.dispatch(event)


# --- 複数プロパティイベントのアトミック性（2026-08-28、取得フェーズと書き込みフェーズの分離） ---
#
# 以前は「プロパティごとに 現在値取得→判定→書き込み」を回していたため、2つ目のプロパティで
# 取得に失敗すると1つ目は既に他ツールへ書き込み済み、という半端な状態が残った（当時は
# Slack通知に「既に適用済みのプロパティ」を載せる対症療法で凌いでいた）。dispatch()を
# 「イベント全体の現在値を取り切ってから書き込む」3フェーズ構成に変えたことで、
# **取得フェーズで失敗した場合は書き込みが1件も発生しない**ことが保証される。
# 単一プロパティのテストではこの保証が自明に成立してしまうため、複数プロパティで検証する。


def test_dispatch_writes_nothing_when_fetch_fails_on_multi_property_event(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """複数プロパティのイベントでも、現在値の取得に失敗したら書き込みは1件も発生しないこと。

    取得はプロパティ単位ではなくイベント単位（ツールごとに1回）で行うため、1つ目のプロパティ
    だけ先に書き込まれてしまう窓が存在しない。
    """
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={
            "CLI-001": {
                "取引先名": "旧名称",
                "住所": "旧住所",
                NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=2),
            }
        },
    )
    zoho = FakeSyncTarget(
        Tool.ZOHO,
        records={"zoho-1": {"取引先名": "旧名称", "住所": "旧住所", "updated_at": NOW - timedelta(hours=3)}},
        get_record_raises=ZohoApiError(500, "internal error"),
    )
    spreadsheet = FakeSyncTarget(
        Tool.SPREADSHEET,
        records={"5": {"取引先名": "旧名称", "住所": "旧住所", "updated_at": NOW - timedelta(hours=3)}},
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = zoho
    targets[Tool.SPREADSHEET] = spreadsheet
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新名称", "住所": "新住所"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "update_target_value_fetch_failed"
    # どのツールにも1件も書き込まれていないこと（これが今回の保証の中核）。
    assert notion.upsert_calls == []
    assert zoho.upsert_calls == []
    assert spreadsheet.upsert_calls == []
    assert targets[Tool.KINTONE].upsert_calls == []
    # last_synced_atは更新されない（次に届く新しいイベントで再処理されるようにするため）。
    assert store.get("CLI-001").last_synced_at == mapping.last_synced_at

    assert len(notifier.update_skipped_calls) == 1
    call = notifier.update_skipped_calls[0]
    assert call["reason"] == "update_target_value_fetch_failed"
    detail = call["detail"]
    # 書き込みが本当にゼロなので、そう断定してよい。
    assert "書き込みは行われていません" in detail
    # 値そのもの（新旧いずれも）は一切含めないこと。
    for leaked_value in ("新名称", "新住所", "旧名称", "旧住所"):
        assert leaked_value not in detail


def test_dispatch_fetches_each_tool_once_for_multi_property_event(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """複数プロパティのイベントでも現在値の取得はツールごとに1回で済むこと。

    以前はプロパティごとに同じレコードを取り直しており、5プロパティなら同じAPIを5回叩いて
    いた。1イベント内の全プロパティが同一スナップショットを見る、という性質も併せて固定する。
    """
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={
            "CLI-001": {
                "取引先名": "旧名称",
                "住所": "旧住所",
                NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=2),
            }
        },
    )
    zoho = FakeSyncTarget(
        Tool.ZOHO,
        records={"zoho-1": {"取引先名": "旧名称", "住所": "旧住所", "updated_at": NOW - timedelta(hours=3)}},
    )
    spreadsheet = FakeSyncTarget(
        Tool.SPREADSHEET,
        records={"5": {"取引先名": "旧名称", "住所": "旧住所", "updated_at": NOW - timedelta(hours=3)}},
    )
    targets = _all_targets()
    targets[Tool.NOTION] = notion
    targets[Tool.ZOHO] = zoho
    targets[Tool.SPREADSHEET] = spreadsheet
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新名称", "住所": "新住所"},
    )

    dispatcher.dispatch(event)

    assert notion.get_record_calls == ["CLI-001"]
    assert zoho.get_record_calls == ["zoho-1"]
    assert spreadsheet.get_record_calls == ["5"]


def test_dispatch_notifies_no_writes_at_all_when_first_property_fetch_fails(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """対照ケース: 1つ目のプロパティで現在値取得が失敗した場合（＝本当に書き込みゼロ）は、
    従来通り「書き込みは行われていません」と断定してよいことの回帰テスト。"""
    targets = _all_targets()
    targets[Tool.NOTION] = FakeSyncTarget(
        Tool.NOTION, get_record_raises=NotionApiError(500, "internal error")
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "新名称", "住所": "新住所"},
    )

    result = dispatcher.dispatch(event)

    assert result.skipped
    assert result.reason == "update_notion_value_fetch_failed"
    for target in targets.values():
        assert target.upsert_calls == []
    assert len(notifier.update_skipped_calls) == 1
    detail = notifier.update_skipped_calls[0]["detail"]
    assert "書き込みは行われていません" in detail
    assert "取引先名" in detail  # 処理中に失敗したプロパティ名


# --- BLOCKER3: Notion・送信元の2者間比較に限定しないコンフリクト検知 -----------------------


def test_conflict_considers_all_sync_scope_tools_not_only_notion_and_source(
    store: SQLiteIdMappingStore, mapping: IdMapping
) -> None:
    """送信元とNotionの値が一致していても、第三のツール（Zoho）が異なる値を持つ場合は
    コンフリクトとして検知できることを確認する（2者間比較のみだとNO_OPに埋もれてしまう）。
    """
    notion = FakeSyncTarget(
        Tool.NOTION, records={"CLI-001": {"取引先名": "A", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=2)}}
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
    # kintone（送信元、NOW時点）がNotion（NOW-2h）より新しいため、たまたま値がNotionと
    # 同じ("A")であっても採用側はkintoneであり、action は PROPAGATE_VALUE になる
    # （最新編集優先ルール。Notion自身の値が最新だったわけではない）。
    assert prop.resolution.action == ResolutionAction.PROPAGATE_VALUE
    assert prop.resolution.resolved_value == "A"
    assert {r.rejected_tool for r in prop.resolution.rejected} == {Tool.ZOHO}
    assert targets[Tool.ZOHO].upsert_calls == [("zoho-1", {"取引先名": "A"})]
    assert targets[Tool.KINTONE].upsert_calls == []  # 送信元は既にNotionと同じ値


# --- BLOCKER2: データ退避（同期ログ）・Slackアラート通知 ---------------------------------


def test_conflict_rejected_data_logged_to_spreadsheet_regardless_of_importance(
    store: SQLiteIdMappingStore,
) -> None:
    """コンフリクト自動解決時の却下データは、重要項目でなくても必ずスプレッドシート
    「同期ログ」タブへ退避されることを確認する（採用側はNotionとは限らない）。"""
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
        records={"MSA-PJ-001": {"案件名": "Notion側案件名", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=1)}},
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
        occurred_at=NOW,  # Notion側のupdated_at（NOW-1h）より新しいため、kintone側が採用される
        properties={"案件名": "kintone側案件名"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.PROPAGATE_VALUE
    assert not prop.resolution.notify_slack  # 「案件名」は重要項目リストに無い

    logged_rows = spreadsheet_client.rows.get(SYNC_LOG_SHEET_NAME, {})
    assert len(logged_rows) == 1
    logged = next(iter(logged_rows.values()))
    assert logged["対象ID"] == "MSA-PJ-001"
    assert logged["項目名"] == "案件名"
    assert logged["採用値"] == "kintone側案件名"
    assert logged["却下値"] == "Notion側案件名"
    assert logged["却下元ツール"] == "notion"


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
        records={"MSA-PJ-002": {"営業ステータス": "商談中(B)", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=1)}},
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
        occurred_at=NOW,  # Notion側のupdated_at（NOW-1h）より新しいため、kintone側が採用される
        properties={"営業ステータス": "失注"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.notify_slack is True
    assert len(notifier.notified) == 1
    assert notifier.notified[0].rejected_value == "商談中(B)"
    assert notifier.notified[0].rejected_tool == Tool.NOTION
    assert notifier.notified[0].adopted_value == "失注"


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
        Tool.NOTION, records={"CLI-001": {"取引先名": "", NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=5)}}
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


# --- 2026-08本番障害の再現（Zohoの案件ステージ変更がNotionへ反映されなかった事故） ----------


def test_zoho_stage_change_newer_than_notion_reaches_notion_end_to_end(
    store: SQLiteIdMappingStore,
) -> None:
    """実際の本番障害の再現テスト: Zohoで案件ステージを「与件整理」→「口頭受注」に変更した
    イベントが、Notion側の古い「与件整理」（更新日時がZoho側の変更より古い）を正しく
    上書きし、written_toolsにTool.NOTIONが含まれ、NotionのSyncTarget.upsert_record()が
    実際に新しい値で呼ばれることを確認する。

    以前は「双方に異なる値が存在する」場合、更新日時に関係なく無条件でNotionの値が
    勝っていたため、この本番障害では written_tools=['zoho'], skipped_tools=['kintone',
    'spreadsheet'] というログになり、Notionがどちらにも現れず変更が失われていた。
    """
    m = IdMapping(
        notion_key="MSA-PJ-100",
        db_key="project",
        kintone_id="3001",
        zoho_id="zoho-100",
        spreadsheet_row=20,
        last_synced_at=NOW - timedelta(days=1),
    )
    store.upsert(m)
    notion = FakeSyncTarget(
        Tool.NOTION,
        records={
            "MSA-PJ-100": {
                "営業ステータス": "与件整理",
                NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=3),
            }
        },
    )
    targets: dict[Tool, Any] = {
        Tool.NOTION: notion,
        Tool.KINTONE: FakeSyncTarget(Tool.KINTONE),
        Tool.ZOHO: FakeSyncTarget(Tool.ZOHO),
        Tool.SPREADSHEET: FakeSyncTarget(Tool.SPREADSHEET),
    }
    dispatcher = Dispatcher(store, targets)
    event = SyncEvent(
        source_tool=Tool.ZOHO,
        db_key="project",
        external_id="zoho-100",
        occurred_at=NOW,  # Notion側のupdated_at（NOW-3h）より新しい
        properties={"営業ステータス": "口頭受注"},
    )

    result = dispatcher.dispatch(event)

    prop = result.properties[0]
    assert prop.resolution.action == ResolutionAction.PROPAGATE_VALUE
    assert prop.resolution.resolved_value == "口頭受注"
    assert Tool.NOTION in prop.written_tools
    assert Tool.NOTION not in prop.skipped_tools
    assert notion.upsert_calls == [("MSA-PJ-100", {"営業ステータス": "口頭受注"})]
    assert targets[Tool.KINTONE].upsert_calls == [("3001", {"営業ステータス": "口頭受注"})]
    assert targets[Tool.SPREADSHEET].upsert_calls == [("20", {"営業ステータス": "口頭受注"})]
    assert targets[Tool.ZOHO].upsert_calls == []  # 送信元には書き戻さない
