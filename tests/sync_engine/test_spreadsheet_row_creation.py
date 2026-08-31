"""スプレッドシートの行を新規作成する経路の検証（2026-08-31）。

**背景。** スプレッドシートのexternal_idは行番号（`IdMapping.spreadsheet_row`）で、
行がまだ無いレコードでは必ずNoneになる。従来はNoneの時点で一律スキップしていたため
**最初の1行を作る経路が存在せず、同期先6シートは全てヘッダ1行のままだった**
（2026-08-31に疎通診断で判明。認証もシート名も正常だったため、到達確認だけでは
検出できなかった）。

ここで固定したいのは3点。
1. **既定では行を作らない。** 有効化すると6万件規模が一気に書かれるため、明示的なオプトインにする
2. **追記した行番号を必ず永続化する。** しないと、同じイベントの次のプロパティでまた
   新しい行が追記され、1レコードにつき行が増え続ける
3. **永続化に失敗したら行番号を持ったmappingを返さない。** 中途半端に持たせると
   「保存されていないのに行があることになっている」状態になる
"""

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
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.production_wiring import (
    SPREADSHEET_ROW_CREATION_ENV_VAR,
    _MultiDbSpreadsheetSyncTarget,
)
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_targets.base import SyncTarget

NOW = datetime(2026, 8, 31, 9, 0, 0, tzinfo=timezone.utc)


class _追記できるスプレッドシート(SyncTarget):
    """本番の`_MultiDbSpreadsheetSyncTarget`と同じ契約を持つテスト用ターゲット。

    external_idがNoneなら新しい行を採番して返し、指定されていればその行を更新する。
    """

    tool = Tool.SPREADSHEET

    def __init__(self) -> None:
        self.upsert_calls: list[tuple[str | None, dict[str, Any]]] = []
        self._next_row = 10

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        return None

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str | None:
        self.upsert_calls.append((external_id, dict(properties)))
        if external_id is None:
            self._next_row += 1
            return str(self._next_row)
        return external_id

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        return None

    @property
    def 追記した回数(self) -> int:
        return sum(1 for external_id, _ in self.upsert_calls if external_id is None)


def _2プロパティのスキーマ() -> DatabaseSchema:
    return DatabaseSchema(
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
                sync_scope=SyncScope.SPREADSHEET_ONLY,
            ),
            PropertyDefinition(
                name="備考",
                property_type=PropertyType.TEXT,
                requirement=RequirementLevel.OPTIONAL,
                sync_scope=SyncScope.SPREADSHEET_ONLY,
            ),
        ),
    )


@pytest.fixture
def store() -> SQLiteIdMappingStore:
    s = SQLiteIdMappingStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def 行がまだ無いmapping(store: SQLiteIdMappingStore) -> IdMapping:
    m = IdMapping(
        notion_key="CLI-001",
        db_key="client_master",
        kintone_id="1001",
        spreadsheet_row=None,
        last_synced_at=NOW - timedelta(days=1),
    )
    store.upsert(m)
    return m


def _2プロパティのイベント() -> SyncEvent:
    return SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW,
        properties={"取引先名": "サンライズホテルズ", "備考": "テスト"},
    )


# --- Dispatcher: 行番号の永続化 -------------------------------------------------------


def test_追記された行番号がIdMappingへ保存される(
    store: SQLiteIdMappingStore,
    行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: _2プロパティのスキーマ())
    sheet = _追記できるスプレッドシート()
    dispatcher = Dispatcher(store, {Tool.SPREADSHEET: sheet})

    dispatcher.dispatch(
        SyncEvent(
            source_tool=Tool.NOTION,
            db_key="client_master",
            external_id="CLI-001",
            occurred_at=NOW,
            properties={"取引先名": "サンライズホテルズ"},
        )
    )

    assert store.get("CLI-001").spreadsheet_row == 11


def test_同じイベントの2つ目のプロパティは同じ行を更新する(
    store: SQLiteIdMappingStore,
    行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**このテストが本体。**行番号を永続化して呼び出し元へ返さないと、
    プロパティの数だけ行が追記され、1レコードが複数行に散らばる。"""
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: _2プロパティのスキーマ())
    sheet = _追記できるスプレッドシート()
    dispatcher = Dispatcher(store, {Tool.SPREADSHEET: sheet})

    dispatcher.dispatch(_2プロパティのイベント())

    assert sheet.追記した回数 == 1, "行が2回追記されている（1レコードが2行に分かれる）"
    assert sheet.upsert_calls == [
        (None, {"取引先名": "サンライズホテルズ"}),
        ("11", {"備考": "テスト"}),
    ]
    assert store.get("CLI-001").spreadsheet_row == 11


def test_行番号の保存に失敗しても同じイベント内では追記を繰り返さない(
    store: SQLiteIdMappingStore,
    行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**シートには既に行が物理的に追記されている**ので、保存に失敗したからといって
    「行が無い」扱いに戻すと、同じイベントの次のプロパティで即座にもう1行追記され、
    1レコードがプロパティの数だけの行に散らばる。

    当初は「保存できていないのに行があることにしない」として更新前のmappingを返していたが、
    Gemini・ChatGPTの両方から独立に「それは重複を増やす」と指摘され修正した（2026-08-31）。
    保存失敗時に残るのは「次回のイベントで1行余分に追記される」可能性だけで、
    こちらの方が明確に被害が小さい。
    """
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: _2プロパティのスキーマ())
    monkeypatch.setattr("src.sync_engine.dispatcher.time.sleep", lambda _s: None)
    sheet = _追記できるスプレッドシート()
    dispatcher = Dispatcher(store, {Tool.SPREADSHEET: sheet})
    monkeypatch.setattr(
        store, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("保存できません"))
    )

    dispatcher.dispatch(_2プロパティのイベント())

    assert sheet.追記した回数 == 1, "保存に失敗しただけで、同じイベント内で2行目が追記されている"
    assert [external_id for external_id, _ in sheet.upsert_calls] == [None, "11"]


def test_追記の直前に他プロセスが作った行を拾う(
    store: SQLiteIdMappingStore,
    行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """並行するWebhookが先に行を作っていた場合、追記の直前に読み直して拾えれば
    2行目を作らずに済む（完全な排他ではないが、窓は大きく狭まる）。"""
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: _2プロパティのスキーマ())
    sheet = _追記できるスプレッドシート()
    dispatcher = Dispatcher(store, {Tool.SPREADSHEET: sheet})

    # ディスパッチ開始後・書き込み直前に、別プロセスが行3を作った状況を作る。
    store.upsert(
        IdMapping(
            notion_key="CLI-001",
            db_key="client_master",
            kintone_id="1001",
            spreadsheet_row=3,
            last_synced_at=行がまだ無いmapping.last_synced_at,
        )
    )

    dispatcher.dispatch(
        SyncEvent(
            source_tool=Tool.NOTION,
            db_key="client_master",
            external_id="CLI-001",
            occurred_at=NOW,
            properties={"取引先名": "サンライズホテルズ"},
        )
    )

    assert sheet.追記した回数 == 0
    assert sheet.upsert_calls == [("3", {"取引先名": "サンライズホテルズ"})]


def test_既に行があるレコードは追記せず更新する(
    store: SQLiteIdMappingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: _2プロパティのスキーマ())
    store.upsert(
        IdMapping(notion_key="CLI-002", db_key="client_master", spreadsheet_row=7)
    )
    sheet = _追記できるスプレッドシート()
    dispatcher = Dispatcher(store, {Tool.SPREADSHEET: sheet})

    dispatcher.dispatch(
        SyncEvent(
            source_tool=Tool.NOTION,
            db_key="client_master",
            external_id="CLI-002",
            occurred_at=NOW,
            properties={"取引先名": "既存"},
        )
    )

    assert sheet.追記した回数 == 0
    assert sheet.upsert_calls == [("7", {"取引先名": "既存"})]
    assert store.get("CLI-002").spreadsheet_row == 7


# --- production_wiring: 既定OFFのフラグ ------------------------------------------------


class _行を数えるシート:
    """`SpreadsheetSyncTarget`の代わり。追記と更新の呼び出しを記録する。"""

    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []
        self.updated: list[tuple[str, dict[str, Any]]] = []

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        return None

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str:
        if external_id is None:
            self.appended.append(dict(properties))
            return "42"
        self.updated.append((external_id, dict(properties)))
        return external_id

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        return None


def test_既定では行を作らない(monkeypatch: pytest.MonkeyPatch) -> None:
    """有効化すると6万件規模が一気に書かれるため、既定は必ずOFF。"""
    monkeypatch.delenv(SPREADSHEET_ROW_CREATION_ENV_VAR, raising=False)
    sheet = _行を数えるシート()
    target = _MultiDbSpreadsheetSyncTarget({"client_master": sheet})

    result = target.upsert_record(None, {"取引先名": "新規"}, db_key="client_master")

    assert result is None
    assert sheet.appended == []


def test_フラグを有効にすると行を追記して行番号を返す(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_ENV_VAR, "true")
    sheet = _行を数えるシート()
    target = _MultiDbSpreadsheetSyncTarget({"client_master": sheet})

    result = target.upsert_record(None, {"取引先名": "新規"}, db_key="client_master")

    assert result == "42"
    assert sheet.appended == [{"取引先名": "新規"}]


def test_フラグが有効でもdb_keyを解決できなければ追記しない(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """どのシートに書くか決まらないまま追記すると、別のDBの行を汚す。"""
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_ENV_VAR, "true")
    sheet = _行を数えるシート()
    target = _MultiDbSpreadsheetSyncTarget({"client_master": sheet})

    assert target.upsert_record(None, {"取引先名": "新規"}, db_key=None) is None
    assert target.upsert_record(None, {"取引先名": "新規"}, db_key="unknown_db") is None
    assert sheet.appended == []


@pytest.mark.parametrize("値", ["", "false", "False", "0", "yes", "TRUE  "])
def test_フラグはtrue以外を有効と解釈しない(値: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """`AUTO_CREATE_NEW_RECORDS_ENABLED`と同じ判定にそろえる（前後の空白は除く）。"""
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_ENV_VAR, 値)
    sheet = _行を数えるシート()
    target = _MultiDbSpreadsheetSyncTarget({"client_master": sheet})

    expected_none = 値.strip().lower() != "true"
    result = target.upsert_record(None, {"取引先名": "新規"}, db_key="client_master")
    assert (result is None) is expected_none
