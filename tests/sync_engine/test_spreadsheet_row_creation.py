"""スプレッドシートの行を新規作成・更新する経路の検証（2026-08-31）。

**背景。** スプレッドシートのexternal_idは行番号で、行がまだ無いレコードでは必ずNoneになる。
従来はNoneの時点で一律スキップしていたため**最初の1行を作る経路が存在せず、同期先6シートは
全てヘッダ1行のままだった**（疎通診断で判明。認証もシート名も正常だったため、
到達確認だけでは検出できなかった）。

行を作れるようにしたうえで、Gemini・ChatGPTのレビューを受けて**行番号を恒久IDとして
信用しない**形に変えた。ここで固定したいのは以下。

1. **既定では行を作らない。**フラグとdb_keyの両方が揃って初めて書く
2. **同じイベント内で行が増えない**（プロパティの数だけ行ができない）
3. **追記したがDBに保存できなかった行を、次回ちゃんと拾う**
   （SheetsとPostgresにまたがるので、try/exceptでは解決できない）
4. **人が行を挿入・削除・並べ替えて行番号がずれても、別レコードを上書きしない**
5. **追記するときは必ず同期キーも書く**（書き忘れた行は次回また重複する）
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
    SPREADSHEET_ROW_CREATION_DB_KEYS_ENV_VAR,
    SPREADSHEET_ROW_CREATION_ENV_VAR,
    _MultiDbSpreadsheetSyncTarget,
)
from src.sync_engine.sync_event import SyncEvent
from src.sync_engine.sync_targets.spreadsheet_sync import SYNC_KEY_COLUMN

NOW = datetime(2026, 8, 31, 9, 0, 0, tzinfo=timezone.utc)


class _シート:
    """1枚のシートを模したFake。行番号 -> 値の辞書を持ち、同期キー列も再現する。

    本物と同じく「行を挿入すると以降の行番号がずれる」ところまで再現できるようにしてある。
    """

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.append_calls = 0

    # --- Dispatcherが使う契約（_MultiDbSpreadsheetSyncTargetと同じ形） ---

    def find_row_by_sync_key(self, sync_key: str) -> int | None:
        for row in sorted(self.rows):
            if self.rows[row].get(SYNC_KEY_COLUMN) == sync_key:
                return row
        return None

    def row_matches_sync_key(self, row: int, sync_key: str) -> bool:
        actual = self.rows.get(row, {}).get(SYNC_KEY_COLUMN)
        # キー未設定の行（この仕組みより前に作られた行）は一致扱い。
        return actual is None or actual == sync_key

    def append_row_with_sync_key(self, properties: dict[str, Any], sync_key: str) -> int:
        self.append_calls += 1
        row = (max(self.rows) if self.rows else 1) + 1
        self.rows[row] = {**properties, SYNC_KEY_COLUMN: sync_key}
        return row

    def with_sync_key(self, properties: dict[str, Any], sync_key: str) -> dict[str, Any]:
        return {**properties, SYNC_KEY_COLUMN: sync_key}

    def upsert_record(
        self, external_id: str | None, properties: dict[str, Any], *, db_key: str | None = None
    ) -> str | None:
        assert external_id is not None, "追記は append_row_with_sync_key を通ること"
        self.rows.setdefault(int(external_id), {}).update(properties)
        return external_id

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        return self.rows.get(int(external_id))

    def delete_record(self, external_id: str, *, db_key: str | None = None) -> None:
        self.rows.pop(int(external_id), None)

    # --- テストから使う操作 ---

    def 人が行を挿入する(self, at: int) -> None:
        """`at`行目に空行を挿入し、それ以降の行番号を1つずつ後ろへずらす。"""
        self.rows = {
            (row + 1 if row >= at else row): values for row, values in self.rows.items()
        }
        self.rows[at] = {}


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


@pytest.fixture(autouse=True)
def 行作成を許可する(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_ENV_VAR, "true")
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_DB_KEYS_ENV_VAR, "client_master")


@pytest.fixture(autouse=True)
def スキーマを差し替える(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: _2プロパティのスキーマ())


@pytest.fixture
def store() -> SQLiteIdMappingStore:
    s = SQLiteIdMappingStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def シート() -> _シート:
    return _シート()


@pytest.fixture
def dispatcher(store: SQLiteIdMappingStore, シート: _シート) -> Dispatcher:
    return Dispatcher(store, {Tool.SPREADSHEET: _MultiDbSpreadsheetSyncTarget({"client_master": シート})})


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


def _イベント(*, 経過分: int = 0, **properties: Any) -> SyncEvent:
    """`経過分`をずらすのは、Dispatcherが`last_synced_at`以前のイベントを
    ループ防止のためスキップするため（2回目以降のイベントは必ず後の時刻にする）。"""
    return SyncEvent(
        source_tool=Tool.NOTION,
        db_key="client_master",
        external_id="CLI-001",
        occurred_at=NOW + timedelta(minutes=経過分),
        properties=properties,
    )


# --- 行の新規作成 -------------------------------------------------------------------------


def test_行が無ければ追記し行番号を保存する(
    dispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート, 行がまだ無いmapping: IdMapping
) -> None:
    dispatcher.dispatch(_イベント(取引先名="サンライズホテルズ"))

    assert シート.append_calls == 1
    row = store.get("CLI-001").spreadsheet_row
    assert row is not None
    assert シート.rows[row]["取引先名"] == "サンライズホテルズ"


def test_追記するときは同期キーも必ず書く(
    dispatcher: Dispatcher, シート: _シート, 行がまだ無いmapping: IdMapping
) -> None:
    """キーを書き忘れた行は、次回また「行が無い」と判断されて重複する。"""
    dispatcher.dispatch(_イベント(取引先名="サンライズホテルズ"))

    assert [values[SYNC_KEY_COLUMN] for values in シート.rows.values()] == ["CLI-001"]


def test_同じイベントの2つ目のプロパティで行が増えない(
    dispatcher: Dispatcher, シート: _シート, 行がまだ無いmapping: IdMapping
) -> None:
    dispatcher.dispatch(_イベント(取引先名="サンライズホテルズ", 備考="テスト"))

    assert シート.append_calls == 1
    assert len(シート.rows) == 1
    (values,) = シート.rows.values()
    assert values["取引先名"] == "サンライズホテルズ"
    assert values["備考"] == "テスト"


def test_2回目のイベントでも行は増えない(
    dispatcher: Dispatcher, シート: _シート, 行がまだ無いmapping: IdMapping
) -> None:
    dispatcher.dispatch(_イベント(取引先名="1回目"))
    dispatcher.dispatch(_イベント(経過分=1, 取引先名="2回目"))

    assert シート.append_calls == 1
    (values,) = シート.rows.values()
    assert values["取引先名"] == "2回目"


# --- 保存失敗・クラッシュからの復帰 ---------------------------------------------------------


def test_行番号をDBに保存できなくても次回に重複しない(
    dispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート, 行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**これがレビューで一番重かった指摘。**
    「Sheetsへの追記は成功したが、行番号をPostgresへ保存する前にプロセスが落ちた」は
    分散トランザクションなので try/except では防げない。
    シート側に同期キーを書いておくことで、次のイベントで拾い直せる。
    """
    monkeypatch.setattr("src.sync_engine.dispatcher.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        store, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("保存できません"))
    )

    dispatcher.dispatch(_イベント(取引先名="1回目"))
    assert シート.append_calls == 1

    # 保存できていないので、次のイベントでもDB上の行番号はNoneのまま。
    monkeypatch.undo()
    dispatcher.dispatch(_イベント(経過分=1, 取引先名="2回目"))

    assert シート.append_calls == 1, "同期キーで拾えず、2行目が追記されている"
    (values,) = シート.rows.values()
    assert values["取引先名"] == "2回目"


# --- 人がシートを触った場合 -----------------------------------------------------------------


def test_人が行を挿入して行番号がずれても別レコードを上書きしない(
    dispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート, 行がまだ無いmapping: IdMapping
) -> None:
    """行番号を恒久IDにしていると、人が1行挿入しただけで隣のレコードを壊す。
    更新前に同期キーを照合し、ずれていたら引き直す。"""
    dispatcher.dispatch(_イベント(取引先名="サンライズホテルズ"))
    元の行 = store.get("CLI-001").spreadsheet_row
    assert 元の行 is not None

    シート.人が行を挿入する(at=元の行)
    ずれた後の行 = 元の行 + 1

    dispatcher.dispatch(_イベント(経過分=1, 取引先名="更新後"))

    assert シート.append_calls == 1, "引き直せず、新しい行が追記されている"
    assert シート.rows[ずれた後の行]["取引先名"] == "更新後"
    assert シート.rows[元の行] == {}, "人が挿入した空行を上書きしている"
    assert store.get("CLI-001").spreadsheet_row == ずれた後の行, "行番号が直っていない"


def test_同期キーが空の行はそのまま使いキーを埋める(
    dispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート
) -> None:
    """この仕組みより前に作られた行（キーが空）は、取り違えではないので
    そのまま使い、書き込みのついでにキーを埋める。"""
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", spreadsheet_row=5))
    シート.rows[5] = {"取引先名": "旧データ"}

    dispatcher.dispatch(_イベント(取引先名="更新後"))

    assert シート.append_calls == 0
    assert シート.rows[5]["取引先名"] == "更新後"
    assert シート.rows[5][SYNC_KEY_COLUMN] == "CLI-001"


# --- 段階的な有効化 -------------------------------------------------------------------------


def test_既定では行を作らない(monkeypatch: pytest.MonkeyPatch, シート: _シート) -> None:
    """有効化すると6万件規模が対象になるため、既定は必ずOFF。"""
    monkeypatch.delenv(SPREADSHEET_ROW_CREATION_ENV_VAR, raising=False)
    monkeypatch.delenv(SPREADSHEET_ROW_CREATION_DB_KEYS_ENV_VAR, raising=False)
    target = _MultiDbSpreadsheetSyncTarget({"client_master": シート})

    assert target.append_with_sync_key({"取引先名": "新規"}, "CLI-001", db_key="client_master") is None
    assert シート.append_calls == 0


def test_フラグだけ立てても対象db_keyを指定しなければ追記しない(
    monkeypatch: pytest.MonkeyPatch, シート: _シート
) -> None:
    """真偽値1つで6万件が対象になるのは事故の範囲が大きすぎる、という指摘への対応
    （Gemini・ChatGPT双方から）。まず件数の少ないDBだけで試せるようにする。"""
    monkeypatch.delenv(SPREADSHEET_ROW_CREATION_DB_KEYS_ENV_VAR, raising=False)
    target = _MultiDbSpreadsheetSyncTarget({"client_master": シート})

    assert target.append_with_sync_key({"取引先名": "新規"}, "CLI-001", db_key="client_master") is None
    assert シート.append_calls == 0


def test_許可されていないdb_keyには追記しない(
    monkeypatch: pytest.MonkeyPatch, シート: _シート
) -> None:
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_DB_KEYS_ENV_VAR, "product")
    target = _MultiDbSpreadsheetSyncTarget({"client_master": シート})

    assert target.append_with_sync_key({"取引先名": "新規"}, "CLI-001", db_key="client_master") is None
    assert シート.append_calls == 0


def test_db_keyを解決できなければ追記しない(
    monkeypatch: pytest.MonkeyPatch, シート: _シート
) -> None:
    """どのシートに書くか決まらないまま追記すると、別のDBの行を汚す。"""
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_DB_KEYS_ENV_VAR, "*")
    target = _MultiDbSpreadsheetSyncTarget({"client_master": シート})

    assert target.append_with_sync_key({"取引先名": "新規"}, "CLI-001", db_key=None) is None
    assert target.append_with_sync_key({"取引先名": "新規"}, "CLI-001", db_key="unknown") is None
    assert シート.append_calls == 0


@pytest.mark.parametrize("値", ["", "false", "False", "0", "yes", "TRUE  "])
def test_フラグはtrue以外を有効と解釈しない(
    値: str, monkeypatch: pytest.MonkeyPatch, シート: _シート
) -> None:
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_ENV_VAR, 値)
    target = _MultiDbSpreadsheetSyncTarget({"client_master": シート})

    result = target.append_with_sync_key({"取引先名": "新規"}, "CLI-001", db_key="client_master")
    assert (result is None) is (値.strip().lower() != "true")


def test_同期キー無しの追記は拒否する(monkeypatch: pytest.MonkeyPatch, シート: _シート) -> None:
    """キー無しで追記すると、その行は次回また「行が無い」と判断されて重複する。"""
    target = _MultiDbSpreadsheetSyncTarget({"client_master": シート})

    assert target.upsert_record(None, {"取引先名": "新規"}, db_key="client_master") is None
    assert シート.append_calls == 0
