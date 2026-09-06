"""シートの行を作れなかったレコードを積んで作り直す仕組みの検証（2026-09-07）。

■ ここで固定したいこと

同期エンジンは「シートの行を作れなかった」ときもWebhookに2xxを返す。そのため
2026-09-03の時点では、**Slackを見落とし、そのレコードが二度と編集されなければ
シートには永久に現れなかった**（ChatGPTがBLOCKER・Geminiが独立にWARN）。

```
   1. 行を作れなかったら積む（新規作成の経路・更新の経路の両方）
   2. **行が既にあるレコードでは積まない**（このキューでは直せないため）
   3. 作り直しは**必ずNotionを読み直す**（古い値で巻き戻さない）
   4. 作り直しの前後で**行が二重にできない**（ロックと再探索）
   5. 混み合っただけの見送りは**試したうちに入れない**（`failed`へ落とさない）
```
"""

from __future__ import annotations

from datetime import datetime, timezone
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
from src.sync_engine import spreadsheet_outbox
from src.sync_engine import spreadsheet_outbox_drain as drain_module
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.spreadsheet_outbox import OutboxEntry
from src.sync_engine.spreadsheet_outbox_drain import drain_spreadsheet_outbox

NOW = datetime(2026, 9, 7, 9, 0, 0, tzinfo=timezone.utc)

_SCHEMA = DatabaseSchema(
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
            name="TEL",
            property_type=PropertyType.TEXT,
            requirement=RequirementLevel.OPTIONAL,
            sync_scope=SyncScope.SPREADSHEET_ONLY,
        ),
    ),
)


class _偽キュー:
    """`spreadsheet_outbox`をメモリ上で再現する（Postgresを使わずに筋を検証する）。"""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}
        self.purged = 0

    # --- 積む側 -------------------------------------------------------------------
    def enqueue_row_creation(self, *, db_key: str, notion_key: str, reason: str) -> bool:
        key = (db_key, notion_key)
        current = self.entries.get(key)
        if current is not None and current["status"] == spreadsheet_outbox.STATUS_PENDING:
            current["reason"] = reason
            return True
        self.entries[key] = {
            "status": spreadsheet_outbox.STATUS_PENDING,
            "reason": reason,
            "attempts": 0,
            "resolution": None,
            "note": None,
        }
        return True

    # --- 取り出す側 ---------------------------------------------------------------
    def claim_due(self, *, db_keys: Any, limit: int) -> list[OutboxEntry]:
        claimed = []
        for (db_key, notion_key), row in self.entries.items():
            if row["status"] != spreadsheet_outbox.STATUS_PENDING or db_key not in db_keys:
                continue
            if len(claimed) >= limit:
                break
            row["attempts"] += 1
            claimed.append(
                OutboxEntry(
                    db_key=db_key,
                    notion_key=notion_key,
                    reason=row["reason"],
                    attempts=row["attempts"],
                    created_at=NOW,
                )
            )
        return claimed

    def mark_done(self, *, db_key: str, notion_key: str, resolution: str, note: str) -> None:
        row = self.entries[(db_key, notion_key)]
        row["status"] = spreadsheet_outbox.STATUS_DONE
        row["resolution"] = resolution
        row["note"] = note

    def list_failed(self, *, db_keys: Any, limit: int) -> list[OutboxEntry]:
        return [
            OutboxEntry(
                db_key=db_key,
                notion_key=notion_key,
                reason=row["reason"],
                attempts=row["attempts"],
                created_at=NOW,
            )
            for (db_key, notion_key), row in list(self.entries.items())[:limit]
            if row["status"] == spreadsheet_outbox.STATUS_FAILED and db_key in db_keys
        ]

    def record_failure(self, *, db_key: str, notion_key: str, error: str) -> str:
        row = self.entries[(db_key, notion_key)]
        row["note"] = error
        if row["attempts"] >= spreadsheet_outbox.MAX_ATTEMPTS:
            row["status"] = spreadsheet_outbox.STATUS_FAILED
            row["resolution"] = spreadsheet_outbox.RESOLUTION_GAVE_UP
        return row["status"]

    def release(self, *, db_key: str, notion_key: str, retry_after_minutes: int) -> None:
        row = self.entries[(db_key, notion_key)]
        row["attempts"] = max(row["attempts"] - 1, 0)

    def purge_resolved(self, days: int = 30) -> int:
        self.purged += 1
        return 0


class _偽シート:
    """同期キーで引ける1枚のシート。追記した順に行番号が増える。"""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self.append_calls = 0

    def find_row_by_sync_key(self, sync_key: str) -> int | None:
        for row, values in self.rows.items():
            if values.get("_sync_key") == sync_key:
                return row
        return None

    def append_row_with_sync_key(self, properties: dict[str, Any], sync_key: str) -> int:
        self.append_calls += 1
        row = len(self.rows) + 2  # 1行目はヘッダ
        self.rows[row] = {**properties, "_sync_key": sync_key}
        return row


class _偽Notion:
    def __init__(self, pages: dict[str, dict[str, Any] | None]) -> None:
        self.pages = pages
        self.get_calls = 0

    def get_page(self, page_id: str) -> dict[str, Any] | None:
        self.get_calls += 1
        return self.pages.get(page_id)


@pytest.fixture(autouse=True)
def _スキーマとフラグを差し替える(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drain_module, "ALL_SCHEMAS", (_SCHEMA,))
    monkeypatch.setattr(drain_module, "get_schema", lambda key: _SCHEMA)
    monkeypatch.setattr(drain_module, "spreadsheet_row_creation_enabled", lambda key: True)


def _キューを差し替える(monkeypatch: pytest.MonkeyPatch, queue: _偽キュー) -> None:
    for name in (
        "claim_due",
        "list_failed",
        "mark_done",
        "record_failure",
        "release",
        "purge_resolved",
    ):
        monkeypatch.setattr(
            drain_module.spreadsheet_outbox, name, getattr(queue, name), raising=True
        )


def _ストア(spreadsheet_row: int | None = None) -> SQLiteIdMappingStore:
    store = SQLiteIdMappingStore()
    store.upsert(
        IdMapping(
            notion_key="page-1",
            db_key="client_master",
            kintone_id="62227",
            spreadsheet_row=spreadsheet_row,
            last_synced_at=NOW,
        )
    )
    return store


def test_積まれた行はNotionの現在値で作り直される(monkeypatch: pytest.MonkeyPatch) -> None:
    """**作成時のスナップショットではなくNotionを読み直す。**

    古い値を貯めて後から流すと、その間に入った新しい値を巻き戻す。
    """
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)
    sheet = _偽シート()
    notion = _偽Notion({"page-1": {"取引先名": "正式名称", "TEL": "03-0000-0000"}})

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": notion},
        spreadsheet_targets={"client_master": sheet},
        slack_notifier=None,
    )

    assert result["created"] == 1
    assert sheet.rows[2]["取引先名"] == "正式名称"
    assert sheet.rows[2]["_sync_key"] == "page-1"
    assert queue.entries[("client_master", "page-1")]["status"] == spreadsheet_outbox.STATUS_DONE


def test_既に行があれば作らずに解決する(monkeypatch: pytest.MonkeyPatch) -> None:
    """次の更新イベントや別のワーカーが先に作っていた場合。**行を二重にしない。**"""
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="update_row_write_skipped"
    )
    _キューを差し替える(monkeypatch, queue)
    sheet = _偽シート()
    sheet.rows[2] = {"取引先名": "先に作られた行", "_sync_key": "page-1"}
    notion = _偽Notion({"page-1": {"取引先名": "古い値"}})

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": notion},
        spreadsheet_targets={"client_master": sheet},
        slack_notifier=None,
    )

    assert result["already_present"] == 1
    assert sheet.append_calls == 0, "既に行があるのに追記した（重複行になる）"
    assert sheet.rows[2]["取引先名"] == "先に作られた行", "古い値で巻き戻した"
    assert notion.get_calls == 0, "行があるならNotionを読む必要すら無い"


def test_Notionが読めない回は諦めずにもう一度(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)

    class _落ちるNotion:
        def get_page(self, page_id: str) -> dict[str, Any] | None:
            raise RuntimeError("boom")

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _落ちるNotion()},
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=None,
    )

    assert result["retry"] == 1
    assert (
        queue.entries[("client_master", "page-1")]["status"] == spreadsheet_outbox.STATUS_PENDING
    )


def test_試行回数を使い切ったら諦めてSlackで人を呼ぶ(monkeypatch: pytest.MonkeyPatch) -> None:
    """**黙って止めない。** 自動で復旧しないものが残ることを人へ伝える。"""
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    queue.entries[("client_master", "page-1")]["attempts"] = spreadsheet_outbox.MAX_ATTEMPTS - 1
    _キューを差し替える(monkeypatch, queue)

    通知: list[dict[str, Any]] = []

    class _通知:
        def notify_update_skipped(self, **kwargs: Any) -> None:
            通知.append(kwargs)

    class _落ちるNotion:
        def get_page(self, page_id: str) -> dict[str, Any] | None:
            raise RuntimeError("boom")

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _落ちるNotion()},
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=_通知(),
    )

    assert result["gave_up"] == 1
    assert queue.entries[("client_master", "page-1")]["status"] == spreadsheet_outbox.STATUS_FAILED
    assert 通知, "打ち切ったのに誰にも知らせていない"
    assert "自動での再試行は止めました" in 通知[0]["detail"]


def test_行の新規作成が許可されていないDBは触らない(monkeypatch: pytest.MonkeyPatch) -> None:
    """許可が無いDBの行を試すと、弾かれるだけで試行回数が空に減って`failed`になる。"""
    monkeypatch.setattr(drain_module, "spreadsheet_row_creation_enabled", lambda key: False)
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={},
        spreadsheet_targets={},
        slack_notifier=None,
    )

    assert result["status"] == "skipped"
    assert (
        queue.entries[("client_master", "page-1")]["status"] == spreadsheet_outbox.STATUS_PENDING
    ), "許可が無いだけなのに諦めてしまっている"


def test_シートへ書ける項目が無ければ行を作らない(monkeypatch: pytest.MonkeyPatch) -> None:
    """同期キーだけの行を作らない（`_append_spreadsheet_row_for_created_record`と同じ判断）。"""
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)
    sheet = _偽シート()

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _偽Notion({"page-1": {}})},
        spreadsheet_targets={"client_master": sheet},
        slack_notifier=None,
    )

    assert result["not_applicable"] == 1
    assert sheet.append_calls == 0


def test_IDマッピングが消えていたら解決として閉じる(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-消えた", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _偽Notion({})},
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=None,
    )

    assert result["not_applicable"] == 1
    assert (
        queue.entries[("client_master", "page-消えた")]["status"]
        == spreadsheet_outbox.STATUS_DONE
    )


def test_時間予算を超えたら残りは差し戻して次の回へ(monkeypatch: pytest.MonkeyPatch) -> None:
    """**取り出したまま放置しない。** 差し戻さないと、触ってもいない行の試行回数が減る。"""
    queue = _偽キュー()
    for i in range(3):
        queue.enqueue_row_creation(
            db_key="client_master", notion_key=f"page-{i}", reason="new_record_row_write_failed"
        )
    _キューを差し替える(monkeypatch, queue)

    result = drain_spreadsheet_outbox(
        budget_seconds=-1.0,  # 最初の1件を処理する前に予算切れ
        store=_ストア(),
        notion_clients={"client_master": _偽Notion({})},
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=None,
    )

    assert result["out_of_budget"] == 3
    for i in range(3):
        row = queue.entries[("client_master", f"page-{i}")]
        assert row["status"] == spreadsheet_outbox.STATUS_PENDING
        assert row["attempts"] == 0, "処理していないのに試行回数が増えたまま"


# --- キュー本体（Postgres）の契約 -------------------------------------------------------


class _偽カーソル:
    """実行されたSQLとパラメータを記録するだけのカーソル。

    **プレースホルダの数と渡した値の数が合っているか**をここで見る。
    ずれていると本番で初めて`ProgrammingError`になり、しかもこのモジュールは
    例外を握って`False`を返すので、**静かに1件も積まれない**状態になる。
    """

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._rows = rows or []
        self.rowcount = len(self._rows)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        assert sql.count("%s") == len(params), (
            f"プレースホルダ{sql.count('%s')}個に対して値が{len(params)}個: {sql}"
        )
        self.executed.append((sql, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def __enter__(self) -> "_偽カーソル":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


class _偽接続:
    def __init__(self, cursor: _偽カーソル) -> None:
        self._cursor = cursor
        self.committed = 0

    def cursor(self) -> _偽カーソル:
        return self._cursor

    def commit(self) -> None:
        self.committed += 1

    def __enter__(self) -> "_偽接続":
        return self

    def __exit__(self, *args: Any) -> None:
        return None


@pytest.fixture(autouse=True)
def _警告をリセットする() -> None:
    spreadsheet_outbox.reset_missing_database_url_warning()


def test_DBが無ければ積めなかったと返す(monkeypatch: pytest.MonkeyPatch) -> None:
    """**Falseを返すことに意味がある。** 呼び出し元はこれを見て
    「自動で作り直します」ではなく「手動のバックフィルが要ります」とSlackへ書く。
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert (
        spreadsheet_outbox.enqueue_row_creation(
            db_key="client_master", notion_key="page-1", reason="test"
        )
        is False
    )


def test_積むSQLのプレースホルダと値の数が合っている(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    cursor = _偽カーソル()
    monkeypatch.setattr(spreadsheet_outbox, "_connect", lambda: _偽接続(cursor))

    assert spreadsheet_outbox.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    sql, params = cursor.executed[0]
    assert "ON CONFLICT" in sql, "同じレコードを2回積んだら行が増える書き方になっている"
    assert params[0] == "client_master"


def test_取り出しのSQLも数が合っている(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    cursor = _偽カーソル(
        [
            {
                "dbKey": "client_master",
                "notionKey": "page-1",
                "reason": "new_record_row_write_failed",
                "attempts": 1,
                "createdAt": NOW,
            }
        ]
    )
    monkeypatch.setattr(spreadsheet_outbox, "_connect", lambda: _偽接続(cursor))

    entries = spreadsheet_outbox.claim_due(db_keys=["client_master"], limit=10)

    assert [e.notion_key for e in entries] == ["page-1"]
    sql, _ = cursor.executed[0]
    assert "FOR UPDATE SKIP LOCKED" in sql, "2つのワーカーが同じ行を掴む"
    assert "attempts = o.attempts + 1" in sql, "取り出しの時点で回数を増やしていない"


def test_許可されたDBが空なら取り出しにいかない(monkeypatch: pytest.MonkeyPatch) -> None:
    """`= ANY('{}')`は何にも当たらないが、そもそも問い合わせない方が速い。"""
    monkeypatch.setenv("DATABASE_URL", "postgres://example")
    cursor = _偽カーソル()
    monkeypatch.setattr(spreadsheet_outbox, "_connect", lambda: _偽接続(cursor))

    assert spreadsheet_outbox.claim_due(db_keys=[], limit=10) == []
    assert cursor.executed == []


# --- 作り直しの残りの分岐（2026-09-07、クマ指摘で追加） ---------------------------------


def test_シートのタブが未設定なら失敗として数える(monkeypatch: pytest.MonkeyPatch) -> None:
    """**構成ミスは差し戻さない**（2026-09-07、シロクマとGeminiが独立に指摘）。

    差し戻し（試行回数を戻す）にすると、恒常的な構成ミスでは試行回数の上限に
    永遠に到達せず、Slackも鳴らず、診断も緑のまま**静かに滞留し続ける**。
    差し戻してよいのは、放っておけば数分で解ける一時的な見送りだけ。
    """
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _偽Notion({})},
        spreadsheet_targets={},  # タブが構成できていない
        slack_notifier=None,
    )

    assert result["retry"] == 1
    row = queue.entries[("client_master", "page-1")]
    assert row["attempts"] == 1, "試行として数えていない（永久に failed にならない）"


def test_Notionクライアントが未設定でも失敗として数える(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={},  # NOTION_API_KEY等が未設定
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=None,
    )

    assert result["retry"] == 1
    assert queue.entries[("client_master", "page-1")]["attempts"] == 1


def test_行作成ロックが取れなければ書かずに差し戻す(monkeypatch: pytest.MonkeyPatch) -> None:
    """**別のワーカーが作成中。ここで追記すると行が重複する。**

    テスト環境は`DATABASE_URL`が無く`acquire_row_creation_lock()`が常にTrueを返すため、
    差し替えないとこの分岐に到達できない（2026-09-07、クマ指摘）。
    """
    from contextlib import contextmanager

    @contextmanager
    def ロックが取れない(db_key: str, notion_key: str) -> Any:
        yield False

    monkeypatch.setattr(drain_module, "acquire_row_creation_lock", ロックが取れない)
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)
    sheet = _偽シート()

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _偽Notion({"page-1": {"取引先名": "新規商事"}})},
        spreadsheet_targets={"client_master": sheet},
        slack_notifier=None,
    )

    assert result["deferred"] == 1
    assert sheet.append_calls == 0, "ロックを取れていないのに追記した（行が重複する）"
    assert queue.entries[("client_master", "page-1")]["attempts"] == 0


def test_行番号の記録に失敗しても行の作成は成功として閉じる(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**シートには既に行が物理的にできている。** ここで失敗扱いにすると、
    次のcronがもう1行作りかねない（`_register_spreadsheet_row()`と同じ判断）。
    """
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)
    store = _ストア()
    monkeypatch.setattr(
        store, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("DBが落ちている"))
    )
    sheet = _偽シート()

    result = drain_spreadsheet_outbox(
        store=store,
        notion_clients={"client_master": _偽Notion({"page-1": {"取引先名": "新規商事"}})},
        spreadsheet_targets={"client_master": sheet},
        slack_notifier=None,
    )

    assert result["created"] == 1
    assert sheet.append_calls == 1
    assert queue.entries[("client_master", "page-1")]["status"] == spreadsheet_outbox.STATUS_DONE


def test_行番号は整数で記録する(monkeypatch: pytest.MonkeyPatch) -> None:
    """`IdMapping.spreadsheet_row`は`int`。文字列を入れると次回の照合が静かに食い違う。"""
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)
    store = _ストア()

    drain_spreadsheet_outbox(
        store=store,
        notion_clients={"client_master": _偽Notion({"page-1": {"取引先名": "新規商事"}})},
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=None,
    )

    assert store.get("page-1").spreadsheet_row == 2


def test_行番号を記録するとき他の列を巻き戻さない(monkeypatch: pytest.MonkeyPatch) -> None:
    """**保存の直前にストアを読み直す**（2026-09-07、シロクマ指摘）。

    ここで持っている mapping は取り出しの時点のスナップショットで、その後
    Sheets・Notion へ何度もAPIを叩いている。その間に別のWebhookが更新した
    `kintone_id` を、古いスナップショットで上書きしてはいけない（lost update）。
    `dispatcher._register_spreadsheet_row()` が 2026-08-31 に同じ指摘で直した形と同じ。
    """
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)
    store = _ストア()

    class _途中で他のWebhookが走るNotion(_偽Notion):
        def get_page(self, page_id: str) -> dict[str, Any] | None:
            # 作り直しの最中に、別の経路が同じレコードを更新した。
            store.upsert(
                IdMapping(
                    notion_key="page-1",
                    db_key="client_master",
                    kintone_id="62227",
                    zoho_id="ZOHO-999",
                    spreadsheet_row=None,
                    last_synced_at=NOW,
                )
            )
            return super().get_page(page_id)

    drain_spreadsheet_outbox(
        store=store,
        notion_clients={
            "client_master": _途中で他のWebhookが走るNotion({"page-1": {"取引先名": "新規商事"}})
        },
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=None,
    )

    保存後 = store.get("page-1")
    assert 保存後.spreadsheet_row == 2
    assert 保存後.zoho_id == "ZOHO-999", "別の経路が書いた値を古いスナップショットで巻き戻した"


@pytest.mark.parametrize(
    "壊すもの",
    ["idマッピングの取得", "シートの検索", "行の追記"],
)
def test_どの段階で落ちても諦めずにもう一度(
    壊すもの: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_fail()`へ落ちる経路はNotionの取得だけではない（2026-09-07、クマ指摘）。"""
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)
    store = _ストア()
    sheet = _偽シート()

    def 落ちる(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    if 壊すもの == "idマッピングの取得":
        monkeypatch.setattr(store, "get", 落ちる)
    elif 壊すもの == "シートの検索":
        monkeypatch.setattr(sheet, "find_row_by_sync_key", 落ちる)
    else:
        monkeypatch.setattr(sheet, "append_row_with_sync_key", 落ちる)

    result = drain_spreadsheet_outbox(
        store=store,
        notion_clients={"client_master": _偽Notion({"page-1": {"取引先名": "新規商事"}})},
        spreadsheet_targets={"client_master": sheet},
        slack_notifier=None,
    )

    assert result["retry"] == 1
    assert (
        queue.entries[("client_master", "page-1")]["status"] == spreadsheet_outbox.STATUS_PENDING
    )


# --- 諦めた行の棚卸し（消えない赤信号を作らない） ----------------------------------------


def test_諦めた行も人が直していれば閉じる(monkeypatch: pytest.MonkeyPatch) -> None:
    """**消えない赤信号は誤報と同じだけ有害**（2026-09-07、おばさん指摘）。

    案内している`backfill_spreadsheet_rows.py`はこのキューを触らないため、
    人が直しても`failed`が残り、診断が永久に赤くなってしまう。
    """
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-直した", reason="new_record_row_write_failed"
    )
    row = queue.entries[("client_master", "page-直した")]
    row["status"] = spreadsheet_outbox.STATUS_FAILED
    _キューを差し替える(monkeypatch, queue)
    sheet = _偽シート()
    sheet.rows[7] = {"取引先名": "人がバックフィルで作った行", "_sync_key": "page-直した"}

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _偽Notion({})},
        spreadsheet_targets={"client_master": sheet},
        slack_notifier=None,
    )

    assert result["recovered_manually"] == 1
    assert queue.entries[("client_master", "page-直した")]["status"] == spreadsheet_outbox.STATUS_DONE
    assert (
        queue.entries[("client_master", "page-直した")]["resolution"]
        == spreadsheet_outbox.RESOLUTION_RECOVERED_MANUALLY
    )


def test_諦めた行がまだ直っていなければ触らない(monkeypatch: pytest.MonkeyPatch) -> None:
    """棚卸しは**確かめるだけ**。自動での再試行はしない（8回試して駄目だったもの）。"""
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-未対応", reason="new_record_row_write_failed"
    )
    queue.entries[("client_master", "page-未対応")]["status"] = spreadsheet_outbox.STATUS_FAILED
    _キューを差し替える(monkeypatch, queue)
    sheet = _偽シート()

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _偽Notion({"page-未対応": {"取引先名": "新規商事"}})},
        spreadsheet_targets={"client_master": sheet},
        slack_notifier=None,
    )

    assert result["recovered_manually"] == 0
    assert sheet.append_calls == 0, "諦めたはずの行を勝手に作り直している"
    assert (
        queue.entries[("client_master", "page-未対応")]["status"]
        == spreadsheet_outbox.STATUS_FAILED
    )


def test_諦めたものが出た回はレスポンスでも分かる(monkeypatch: pytest.MonkeyPatch) -> None:
    """cronの応答が一律`success`だと「成功＝問題なし」に読める（2026-09-07、おばさん指摘）。"""
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    queue.entries[("client_master", "page-1")]["attempts"] = spreadsheet_outbox.MAX_ATTEMPTS - 1
    _キューを差し替える(monkeypatch, queue)

    class _落ちるNotion2:
        def get_page(self, page_id: str) -> dict[str, Any] | None:
            raise RuntimeError("boom")

    result = drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _落ちるNotion2()},
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=None,
    )

    assert result["needs_attention"] is True


def test_解決の内訳を残す(monkeypatch: pytest.MonkeyPatch) -> None:
    """`done`の件数だけでは「行を作れた」と「そもそも要らなかった」が混ざる。"""
    queue = _偽キュー()
    queue.enqueue_row_creation(
        db_key="client_master", notion_key="page-1", reason="new_record_row_write_failed"
    )
    _キューを差し替える(monkeypatch, queue)

    drain_spreadsheet_outbox(
        store=_ストア(),
        notion_clients={"client_master": _偽Notion({"page-1": {"取引先名": "新規商事"}})},
        spreadsheet_targets={"client_master": _偽シート()},
        slack_notifier=None,
    )

    assert (
        queue.entries[("client_master", "page-1")]["resolution"]
        == spreadsheet_outbox.RESOLUTION_CREATED
    )
