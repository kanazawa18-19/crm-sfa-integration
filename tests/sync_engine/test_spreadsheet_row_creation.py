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
import requests

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
    Tool,
)
from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY
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
        self.update_calls = 0

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
        self,
        external_id: str | None,
        properties: dict[str, Any],
        *,
        db_key: str | None = None,
        expected_version: str | None = None,
    ) -> str | None:
        assert external_id is not None, "追記は append_row_with_sync_key を通ること"
        self.update_calls += 1
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


# --- 並行実行の排他 -------------------------------------------------------------------------


def test_別のワーカーが作成中なら追記を見送る(
    dispatcher: Dispatcher, シート: _シート, 行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ロックが取れないのは、別のワーカーがまさに同じレコードの行を作っている最中。
    そこで追記すると重複するので見送る。次の同期イベントで同期キーから引ける。"""
    from contextlib import contextmanager

    @contextmanager
    def 取れないロック(db_key: str, notion_key: str):
        yield False

    monkeypatch.setattr("src.sync_engine.dispatcher.acquire_row_creation_lock", 取れないロック)

    result = dispatcher.dispatch(_イベント(取引先名="サンライズホテルズ"))

    assert シート.append_calls == 0
    assert result.properties[0].skipped_tools == frozenset({Tool.SPREADSHEET})


def test_ロック取得後にもう一度探してから追記する(
    dispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート,
    行がまだ無いmapping: IdMapping, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ロックを待っている間に相手が作り終えていることがある。
    取得後にもう一度探さないと、結局2行になる。"""
    from contextlib import contextmanager

    @contextmanager
    def 待っている間に相手が作る(db_key: str, notion_key: str):
        シート.rows[9] = {"取引先名": "相手が作った", SYNC_KEY_COLUMN: notion_key}
        yield True

    monkeypatch.setattr(
        "src.sync_engine.dispatcher.acquire_row_creation_lock", 待っている間に相手が作る
    )

    dispatcher.dispatch(_イベント(取引先名="こちらの値"))

    assert シート.append_calls == 0, "相手が作った行を見落として追記している"
    assert シート.rows[9]["取引先名"] == "こちらの値"
    assert store.get("CLI-001").spreadsheet_row == 9


def test_同期キー無しの追記は拒否する(monkeypatch: pytest.MonkeyPatch, シート: _シート) -> None:
    """キー無しで追記すると、その行は次回また「行が無い」と判断されて重複する。"""
    target = _MultiDbSpreadsheetSyncTarget({"client_master": シート})

    assert target.upsert_record(None, {"取引先名": "新規"}, db_key="client_master") is None
    assert シート.append_calls == 0


def test_既にある行への更新も複数プロパティを1回でまとめる(
    dispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート, 行がまだ無いmapping: IdMapping
) -> None:
    """**追記だけでなく更新でも1回にまとめること**（2026-09-01、kuma-qaレビューWARN）。

    追記（append）と更新（update）は別の分岐なので、追記側だけ検証していると
    更新側のまとめ漏れに気づけない。
    """
    dispatcher.dispatch(_イベント(取引先名="サンライズホテルズ"))
    行 = store.get("CLI-001").spreadsheet_row
    before = シート.update_calls

    dispatcher.dispatch(_イベント(経過分=1, 取引先名="更新後", 備考="メモ"))

    assert シート.update_calls == before + 1, "プロパティごとに更新している"
    assert シート.rows[行]["取引先名"] == "更新後"
    assert シート.rows[行]["備考"] == "メモ"
    assert シート.append_calls == 1, "更新なのに行が増えている"


# --- 行を作るときは全項目を埋める（2026-09-02） -----------------------------------------------
#
# **背景。** Webhookは差分しか運ばない。行がまだ無いレコードでNotion側の1項目だけを編集すると、
# その1列と同期キーだけの行が追記されていた（取引先名もkintone IDも空の行）。
# kintone発でも同じで、Webhookは全項目を運ぶものの、Notionと値が一致する項目は競合判定で
# NO_OPになって落ちるため、結局「変わった項目」だけの行になる。
# 行を作るときに限りNotionから全項目を取り直す。


class _Notion:
    """Notionページを模したFake。取得回数を数えて「取り直したか」を検証できるようにする。"""

    def __init__(self, record: dict[str, Any] | None) -> None:
        self._record = record
        self.get_calls = 0
        self.raises: Exception | None = None
        self.upserts: list[tuple[str | None, dict[str, Any]]] = []

    def get_record(self, external_id: str, *, db_key: str | None = None) -> dict[str, Any] | None:
        self.get_calls += 1
        if self.raises is not None:
            raise self.raises
        return dict(self._record) if self._record is not None else None

    def upsert_record(
        self,
        external_id: str | None,
        properties: dict[str, Any],
        *,
        db_key: str | None = None,
        expected_version: str | None = None,
    ) -> str | None:
        self.upserts.append((external_id, properties))
        return external_id


def _Notionの現在値(**properties: Any) -> dict[str, Any]:
    """Notionページの現在値。最終更新はイベントより前にしておく
    （そうしないと競合判定でNotion側が勝ち、書き込み先が変わってしまう）。"""
    return {**properties, NOTION_LAST_EDITED_TIME_KEY: NOW - timedelta(hours=1)}


@pytest.fixture
def notion() -> _Notion:
    return _Notion(_Notionの現在値(取引先名="サンライズホテルズ", 備考="既存のメモ"))


@pytest.fixture
def notion付きdispatcher(
    store: SQLiteIdMappingStore, シート: _シート, notion: _Notion
) -> Dispatcher:
    return Dispatcher(
        store,
        {
            Tool.NOTION: notion,
            Tool.SPREADSHEET: _MultiDbSpreadsheetSyncTarget({"client_master": シート}),
        },
    )


def test_行を作るときはNotionから全項目を取り直して埋める(
    notion付きdispatcher: Dispatcher, シート: _シート, notion: _Notion, 行がまだ無いmapping: IdMapping
) -> None:
    """1項目だけのイベントでも、できる行は全項目そろっている。"""
    notion付きdispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert シート.append_calls == 1
    (values,) = シート.rows.values()
    assert values["備考"] == "更新後のメモ", "イベント側の値が優先されていない"
    assert values["取引先名"] == "サンライズホテルズ", "★ここが本題。1列だけの行になっている"
    assert values[SYNC_KEY_COLUMN] == "CLI-001"


def test_行があるときは取り直さない(
    notion付きdispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート, notion: _Notion
) -> None:
    """更新は従来どおり差分だけ。毎回Notionを読みに行くと無駄なAPI呼び出しが増える。"""
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", spreadsheet_row=2))
    シート.rows[2] = {"取引先名": "既存", SYNC_KEY_COLUMN: "CLI-001"}

    notion付きdispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert notion.get_calls == 0, "行があるのにNotionを読み直している"
    assert シート.rows[2]["取引先名"] == "既存", "差分更新なのに他の列が書き換わっている"
    assert シート.rows[2]["備考"] == "更新後のメモ"


def test_Notionが読めないときは欠けた行を作らない(
    notion付きdispatcher: Dispatcher, シート: _シート, notion: _Notion, 行がまだ無いmapping: IdMapping
) -> None:
    """欠けた行を作るより、行が無いまま残すほうがよい。
    `verify_spreadsheet_backfill.py`の「不足」に出るし、次のイベントでも作り直せる。"""
    notion._record = None

    notion付きdispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert シート.append_calls == 0
    assert シート.rows == {}


def test_Notionの取得が失敗したときも欠けた行を作らない(
    notion付きdispatcher: Dispatcher, シート: _シート, notion: _Notion, 行がまだ無いmapping: IdMapping
) -> None:
    notion.raises = requests.exceptions.ConnectionError("接続できません")

    notion付きdispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert シート.append_calls == 0
    assert シート.rows == {}


def test_次のイベントで作り直せる(
    notion付きdispatcher: Dispatcher, シート: _シート, notion: _Notion, 行がまだ無いmapping: IdMapping
) -> None:
    """1回落ちても取り返しがつくこと（行が無いままなので、次も追記経路を通る）。"""
    notion.raises = requests.exceptions.ConnectionError("接続できません")
    notion付きdispatcher.dispatch(_イベント(備考="1回目"))
    assert シート.append_calls == 0

    notion.raises = None
    notion付きdispatcher.dispatch(_イベント(経過分=1, 備考="2回目"))

    assert シート.append_calls == 1
    (values,) = シート.rows.values()
    assert values["備考"] == "2回目"
    assert values["取引先名"] == "サンライズホテルズ"


def test_行を作らない設定ならNotionを読みに行かない(
    monkeypatch: pytest.MonkeyPatch,
    notion付きdispatcher: Dispatcher,
    シート: _シート,
    notion: _Notion,
    行がまだ無いmapping: IdMapping,
) -> None:
    """どうせ書けないのだから取り直すだけ無駄。"""
    monkeypatch.setenv(SPREADSHEET_ROW_CREATION_DB_KEYS_ENV_VAR, "product")

    notion付きdispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert notion.get_calls == 0
    assert シート.append_calls == 0


def test_kintone発でも一致した項目が落ちて1列だけにならない(
    notion付きdispatcher: Dispatcher, シート: _シート, 行がまだ無いmapping: IdMapping
) -> None:
    """kintone Webhookは全項目を運ぶが、**Notionと値が一致する項目は競合判定でNO_OPになり
    書き込み対象から落ちる**。そのままでは変わった項目だけの行ができる。"""
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"取引先名": "サンライズホテルズ", "備考": "kintoneで書き換えた"},
    )

    notion付きdispatcher.dispatch(event)

    assert シート.append_calls == 1
    (values,) = シート.rows.values()
    assert values["備考"] == "kintoneで書き換えた"
    assert values["取引先名"] == "サンライズホテルズ", "NO_OPで落ちた項目が空欄のままになっている"


# --- 全項目を使ってよいのは「追記」だけ（2026-09-02、シロクマ・クマが独立に指摘） -------------
#
# 補完した全項目を書き込みペイロードごと差し替えると、**追記のつもりが更新に化ける経路**へ
# 流れ込む。今回のイベントで触っていない列を、取得時点のNotionの値で巻き戻してしまう。


def test_行番号が未登録でも既に行があるなら他の列を巻き戻さない(
    notion付きdispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート
) -> None:
    """行番号の保存に失敗した後・`--skip-id-mapping`でバックフィルした後に起きる状態。
    `spreadsheet_row`はNoneだが、シートには同期キー付きの行が既にある。"""
    store.upsert(
        IdMapping(
            notion_key="CLI-001",
            db_key="client_master",
            spreadsheet_row=None,
            last_synced_at=NOW - timedelta(days=1),
        )
    )
    シート.rows[5] = {
        "取引先名": "シート側で直された新しい社名",
        SYNC_KEY_COLUMN: "CLI-001",
    }

    notion付きdispatcher.dispatch(_イベント(備考="今回の編集"))

    assert シート.append_calls == 0
    assert シート.rows[5]["備考"] == "今回の編集"
    assert (
        シート.rows[5]["取引先名"] == "シート側で直された新しい社名"
    ), "触っていない列が古いNotionの値で巻き戻っている"


def test_ロック待ちの間に相手が作った行を巻き戻さない(
    notion付きdispatcher: Dispatcher, シート: _シート, 行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """こちらがNotionを読んだ後に、別のワーカーが先に行を作り終えることがある。
    そこへ「こちらが読んだ時点の全項目」を書くと、相手の新しい値を消す。"""
    from contextlib import contextmanager

    @contextmanager
    def 待っている間に相手が作る(db_key: str, notion_key: str):
        シート.rows[9] = {"取引先名": "相手が書いた新社名", SYNC_KEY_COLUMN: notion_key}
        yield True

    monkeypatch.setattr(
        "src.sync_engine.dispatcher.acquire_row_creation_lock", 待っている間に相手が作る
    )

    notion付きdispatcher.dispatch(_イベント(備考="こちらの備考"))

    assert シート.append_calls == 0, "相手が作った行を見落として追記している"
    assert シート.rows[9]["備考"] == "こちらの備考"
    assert (
        シート.rows[9]["取引先名"] == "相手が書いた新社名"
    ), "相手の値を古いNotionのスナップショットで上書きしている"


# --- 補完の中身 ---------------------------------------------------------------------------


def test_リレーションだけしか補えないなら数に入れない(
    store: SQLiteIdMappingStore,
    シート: _シート,
    行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """リレーションは`drop_relation_properties()`で必ず落とされる。
    落とされるものを「補えた」と数えると、実際は1列だけの行なのに直ったつもりになる。"""
    schema = DatabaseSchema(
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
                name="チェーン",
                property_type=PropertyType.RELATION,
                requirement=RequirementLevel.OPTIONAL,
                sync_scope=SyncScope.SPREADSHEET_ONLY,
                relation_target="chain",
            ),
        ),
    )
    # `drop_relation_properties()`は書き込み側のモジュールで`get_schema`を引くので、
    # そちらも差し替えないと本物のスキーマを見に行ってしまう。
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: schema)
    monkeypatch.setattr(
        "src.sync_engine.sync_targets.spreadsheet_sync.get_schema", lambda key: schema
    )
    notion = _Notion(_Notionの現在値(取引先名="サンライズホテルズ", チェーン=["ページID"]))
    dispatcher = Dispatcher(
        store,
        {
            Tool.NOTION: notion,
            Tool.SPREADSHEET: _MultiDbSpreadsheetSyncTarget({"client_master": シート}),
        },
    )

    dispatcher.dispatch(_イベント(取引先名="サンライズホテルズ"))

    assert シート.append_calls == 1
    (values,) = シート.rows.values()
    assert "チェーン" not in values, "シートに書けないリレーションを書こうとしている"


def test_Notion側が空欄の項目は空欄のまま書く(
    notion付きdispatcher: Dispatcher, シート: _シート, notion: _Notion, 行がまだ無いmapping: IdMapping
) -> None:
    """Notionで未入力なら、シートも空欄でよい。**推測で埋めない。**"""
    notion._record = _Notionの現在値(取引先名=None, 備考="既存のメモ")

    notion付きdispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert シート.append_calls == 1
    (values,) = シート.rows.values()
    assert values["取引先名"] is None


# --- 送信元ごとの経路 ----------------------------------------------------------------------


def test_Zoho発でも補う(
    notion付きdispatcher: Dispatcher, store: SQLiteIdMappingStore, シート: _シート, notion: _Notion
) -> None:
    """kintoneと同じコードを通るが、経路として1本は固定しておく。"""
    store.upsert(
        IdMapping(
            notion_key="CLI-001",
            db_key="client_master",
            zoho_id="Z-1",
            last_synced_at=NOW - timedelta(days=1),
        )
    )
    event = SyncEvent(
        source_tool=Tool.ZOHO,
        db_key="client_master",
        external_id="Z-1",
        occurred_at=NOW,
        properties={"備考": "Zohoで書き換えた"},
    )

    notion付きdispatcher.dispatch(event)

    assert シート.append_calls == 1
    (values,) = シート.rows.values()
    assert values["備考"] == "Zohoで書き換えた"
    assert values["取引先名"] == "サンライズホテルズ"


def test_kintone発では取得済みのスナップショットを使い回す(
    notion付きdispatcher: Dispatcher, シート: _シート, notion: _Notion, 行がまだ無いmapping: IdMapping
) -> None:
    """非Notion発の経路はフェーズ2で既にNotionを読んでいる。二度読みしない。"""
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"備考": "kintoneで書き換えた"},
    )

    notion付きdispatcher.dispatch(event)

    assert notion.get_calls == 1, "同じイベントでNotionを2回読んでいる"


def test_kintone発でNotionページが読めなければ行を作らない(
    notion付きdispatcher: Dispatcher, シート: _シート, notion: _Notion, 行がまだ無いmapping: IdMapping
) -> None:
    """非Notion発の経路（フェーズ2の取得結果がNone）でも、欠けた行は作らない。"""
    notion._record = None
    event = SyncEvent(
        source_tool=Tool.KINTONE,
        db_key="client_master",
        external_id="1001",
        occurred_at=NOW,
        properties={"備考": "kintoneで書き換えた"},
    )

    notion付きdispatcher.dispatch(event)

    assert シート.append_calls == 0
    assert シート.rows == {}


def test_スプレッドシート発では補わない(
    notion付きdispatcher: Dispatcher, シート: _シート, notion: _Notion, 行がまだ無いmapping: IdMapping
) -> None:
    """送信元は自己除外されるので、そもそもシートへは書かない（無駄な取得もしない）。"""
    event = SyncEvent(
        source_tool=Tool.SPREADSHEET,
        db_key="client_master",
        external_id="5",
        occurred_at=NOW,
        properties={"備考": "シートで書き換えた"},
    )

    notion付きdispatcher.dispatch(event)

    assert シート.append_calls == 0


def test_スキーマが引けなければ行を作らない(
    store: SQLiteIdMappingStore,
    シート: _シート,
    行がまだ無いmapping: IdMapping,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定漏れ・デプロイ不整合でスキーマが引けないとき「従来どおり書く」に倒すと、
    今直した不具合（1列だけの行）をそこだけ静かに再発させる（ChatGPTのクロスレビュー指摘）。

    dispatch()の冒頭でも同じ`get_schema`を引くので、**補完のときだけ**引けなくなる状況を作る。
    """
    呼ばれた回数 = {"n": 0}

    def たまに引けない(key: str):
        呼ばれた回数["n"] += 1
        if 呼ばれた回数["n"] >= 2:
            raise KeyError(key)
        return _2プロパティのスキーマ()

    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", たまに引けない)
    notion = _Notion(_Notionの現在値(取引先名="サンライズホテルズ"))
    dispatcher = Dispatcher(
        store,
        {
            Tool.NOTION: notion,
            Tool.SPREADSHEET: _MultiDbSpreadsheetSyncTarget({"client_master": シート}),
        },
    )

    dispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert シート.append_calls == 0
    assert シート.rows == {}


class _kintoneのFake:
    """kintone側は書けたことだけ分かればよい。"""

    def __init__(self) -> None:
        self.writes: list[dict[str, Any]] = []

    def get_record(self, external_id: str, *, db_key: str | None = None):
        return None

    def upsert_record(self, external_id, properties, *, db_key=None, expected_version=None):
        self.writes.append(properties)
        return external_id or "1001"


def test_補えなかったときはシートだけ見送り他ツールへは書く(
    store: SQLiteIdMappingStore, シート: _シート, notion: _Notion,
    行がまだ無いmapping: IdMapping, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**シートの行作成だけを best-effort に落としている。**
    Notionが読めないからといってkintoneへの伝播まで止めると、差分しか運ばれない
    Webhookではその変更が二度と届かない。見送りは`skipped_tools`に出す。"""
    schema = DatabaseSchema(
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
                name="備考",
                property_type=PropertyType.TEXT,
                requirement=RequirementLevel.OPTIONAL,
                sync_scope=SyncScope.ALL_TOOLS,
            ),
        ),
    )
    monkeypatch.setattr("src.sync_engine.dispatcher.get_schema", lambda key: schema)
    kintone = _kintoneのFake()
    notion._record = None
    dispatcher = Dispatcher(
        store,
        {
            Tool.NOTION: notion,
            Tool.KINTONE: kintone,
            Tool.SPREADSHEET: _MultiDbSpreadsheetSyncTarget({"client_master": シート}),
        },
    )

    result = dispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert シート.append_calls == 0, "欠けた行を作っている"
    assert kintone.writes == [{"備考": "更新後のメモ"}], "他ツールへの伝播まで止めている"
    # Zohoはこのテストで接続していないので、そちらも「書けなかった」に入る。見るのはシート。
    assert (
        Tool.SPREADSHEET in result.properties[0].skipped_tools
    ), "見送りが報告に出ていない（Slack通知にも乗らない）"
    assert Tool.KINTONE in result.properties[0].written_tools


def test_行を作ったときは補完した項目も書いたと報告する(
    notion付きdispatcher: Dispatcher, シート: _シート, 行がまだ無いmapping: IdMapping
) -> None:
    """`written_tools`はAPIの応答とログに出る。実際に書いた項目とズレていると、
    障害調査のときに「書いていないはずの列が入っている」と読み違える。"""
    result = notion付きdispatcher.dispatch(_イベント(備考="更新後のメモ"))

    assert result.properties[0].written_tools == frozenset({Tool.SPREADSHEET})
    assert シート.rows[2]["取引先名"] == "サンライズホテルズ"
