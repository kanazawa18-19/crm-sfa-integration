"""scripts/replay_zoho_missed_updates.py の単体テスト。
Zoho・Notion・Postgres へは一切到達させない（クライアント・Dispatcher はすべて偽物）。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY  # 文字列を直書きしない（Gemini レビュー BLOCKER）

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("replay_zoho_missed_updates", _SCRIPTS / "replay_zoho_missed_updates.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)

JST = timezone(timedelta(hours=9))
SINCE = datetime(2026, 9, 15, 16, 0, tzinfo=JST)


def _entry(action: str, at: str, fields: list[tuple[str, str, str]] | None = None) -> dict:
    return {
        "action": action,
        "audited_time": at,
        "field_history": [{"api_name": a, "_value": {"old": o, "new": n}} for a, o, n in (fields or [])],
    }


# --- Timeline → 変わった項目 ------------------------------------------------------------------


def test_changed_fields_keeps_latest_time_and_first_old_value() -> None:
    entries = [  # Timeline は新しい順に返る
        _entry("slack_notification", "2026-09-24T17:32:08+09:00"),
        _entry("updated", "2026-09-24T17:32:07+09:00", [("Stage", "回答待ち", "口頭受注")]),
        _entry("updated", "2026-09-20T10:00:00+09:00", [("Stage", "商談済み", "回答待ち"), ("Probability", "30", "50")]),
        _entry("updated", "2026-09-07T18:52:38+09:00", [("Deal_Name", "旧", "新")]),  # since より前
        _entry("added", "2026-09-01T00:00:00+09:00"),
    ]
    found = mod.changed_fields_from_timeline(entries, SINCE)
    assert set(found) == {"Stage", "Probability"}
    assert mod.changed_fields_from_timeline(list(reversed(entries)), SINCE)["Stage"].display_old == "商談済み"  # 並びに依存しない
    stage = found["Stage"]
    assert stage.audited_at == datetime(2026, 9, 24, 17, 32, 7, tzinfo=JST)
    assert (stage.display_old, stage.display_new) == ("商談済み", "口頭受注")
    assert found["Probability"].display_new == "50"


def test_changed_fields_respects_until_and_blank_values() -> None:
    entries = [
        _entry("updated", "2026-09-27T09:00:00+09:00", [("Stage", "a", "b")]),  # until より後
        _entry("updated", "2026-09-20T10:00:00+09:00", [("field1", " ", "￥ 20,000")]),
    ]
    found = mod.changed_fields_from_timeline(entries, SINCE, until=datetime(2026, 9, 26, 17, 44, tzinfo=JST))
    assert set(found) == {"field1"}
    assert found["field1"].display_old is None  # 空白だけの表示値は「空」


def test_parse_zoho_time_requires_timezone() -> None:
    with pytest.raises(ValueError):
        mod.parse_zoho_time("2026-09-24T17:32:07")


# --- 項目の分類 -------------------------------------------------------------------------------


def _change(audited: datetime, converted, notion) -> "mod.FieldChange":
    return mod.FieldChange(api_name="Stage", audited_at=audited, converted_value=converted, notion_value=notion)


def test_classify_order_unmapped_missing_not_converted() -> None:
    c = _change(SINCE, "x", "y")
    kw = dict(notion_last_edited_at=None, in_record=True, mapped=True, converted=True)
    assert mod.classify_field(c, **{**kw, "mapped": False}) == mod.FIELD_UNMAPPED
    assert mod.classify_field(c, **{**kw, "in_record": False}) == mod.FIELD_MISSING_IN_RECORD
    assert mod.classify_field(c, **{**kw, "converted": False}) == mod.FIELD_NOT_CONVERTED  # 時刻不明＝安全側


def test_classify_relation_pending_only_when_notion_untouched() -> None:
    """名寄せが要る項目は、Notion が後から触られていないときだけ --apply で解決を試みる。"""
    zoho_at = datetime(2026, 9, 24, 17, 32, 7, tzinfo=JST)
    c = _change(zoho_at, None, None)
    kw = dict(in_record=True, mapped=True, converted=False)
    assert mod.classify_field(c, notion_last_edited_at=datetime(2026, 9, 24, 17, 31, tzinfo=JST), **kw) == mod.FIELD_RELATION_PENDING
    assert mod.classify_field(c, notion_last_edited_at=datetime(2026, 9, 24, 17, 32, tzinfo=JST), **kw) == mod.FIELD_NOT_CONVERTED
    r = mod.RecordReplay(zoho_id="1", module="Deals", db_key="project", notion_key="n")
    r.fields = [mod.FieldChange(api_name="field5", audited_at=SINCE, status=mod.FIELD_RELATION_PENDING)]
    assert mod.record_outcome(r) == mod.RECORD_READY and len(r.replayable()) == 1


def test_classify_already_synced_treats_empty_variants_as_equal() -> None:
    kw = dict(notion_last_edited_at=None, in_record=True, mapped=True, converted=True)
    assert mod.classify_field(_change(SINCE, "口頭受注", "口頭受注"), **kw) == mod.FIELD_ALREADY_SYNCED
    assert mod.classify_field(_change(SINCE, None, ""), **kw) == mod.FIELD_ALREADY_SYNCED


def test_classify_safe_only_when_notion_untouched_before_the_minute_of_zoho_change() -> None:
    zoho_at = datetime(2026, 9, 24, 17, 32, 7, tzinfo=JST)
    kw = dict(in_record=True, mapped=True, converted=True)
    c = _change(zoho_at, "口頭受注", "回答待ち")
    # Notion は分単位に丸められる。前の分なら安全、同じ分・後なら曖昧、不明なら曖昧
    assert mod.classify_field(c, notion_last_edited_at=datetime(2026, 9, 24, 17, 31, tzinfo=JST), **kw) == mod.FIELD_SAFE
    assert mod.classify_field(c, notion_last_edited_at=datetime(2026, 9, 24, 17, 32, tzinfo=JST), **kw) == mod.FIELD_AMBIGUOUS
    assert mod.classify_field(c, notion_last_edited_at=datetime(2026, 9, 25, 0, 0, tzinfo=JST), **kw) == mod.FIELD_AMBIGUOUS
    assert mod.classify_field(c, notion_last_edited_at=None, **kw) == mod.FIELD_AMBIGUOUS
    # タイムゾーンが違っても正しく比べる（UTC 08:31 = JST 17:31）
    assert mod.classify_field(c, notion_last_edited_at=datetime(2026, 9, 24, 8, 31, tzinfo=timezone.utc), **kw) == mod.FIELD_SAFE


def test_record_outcome_and_force_flag() -> None:
    r = mod.RecordReplay(zoho_id="1", module="Deals", db_key="project", notion_key="n")
    assert mod.record_outcome(r) == mod.RECORD_NOTHING_TO_DO
    r.fields = [mod.FieldChange(api_name="Stage", audited_at=SINCE, status=mod.FIELD_AMBIGUOUS)]
    assert mod.record_outcome(r) == mod.RECORD_NEEDS_REVIEW
    assert r.replayable() == []
    r.forced = True
    assert mod.record_outcome(r) == mod.RECORD_READY
    assert len(r.replayable()) == 1
    r.error = "boom"
    assert mod.record_outcome(r) == mod.RECORD_ERROR


def test_build_notification_payload_matches_zoho_shape() -> None:
    at = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    payload = mod.build_notification_payload("Deals", "22334", {"Stage": "口頭受注"}, at)
    assert payload == {
        "module": "Deals",
        "ids": ["22334"],
        "affected_values": [{"record_id": "22334", "values": {"Stage": "口頭受注"}}],
        "server_time": int(at.timestamp() * 1000),
    }


def test_build_notification_payload_is_accepted_by_production_converter() -> None:
    """本番の変換（zoho_payload_to_sync_events）に通り、案件の「ステージ」が「営業ステータス」になる。"""
    from src.sync_engine.webhook_handlers.zoho_webhook import zoho_payload_to_sync_events

    at = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    events = zoho_payload_to_sync_events(mod.build_notification_payload("Deals", "1", {"Stage": "口頭受注"}, at), {})
    assert len(events) == 1
    assert events[0].properties == {"営業ステータス": "口頭受注"}
    assert events[0].occurred_at == at


def test_resolve_notion_property_uses_mapping_tables() -> None:
    assert mod.resolve_notion_property("Deals", "project", "Stage") == ("ステージ", "営業ステータス")
    assert mod.resolve_notion_property("Deals", "project", "no_such_field") == (None, None)


def test_exit_code_reflects_errors_in_dry_run_and_skips_in_apply() -> None:
    def rec(outcome: str) -> "mod.RecordReplay":
        return mod.RecordReplay(zoho_id="1", module="Deals", db_key="project", notion_key="n", outcome=outcome)

    assert mod.exit_code_for([rec(mod.RECORD_ERROR)], apply=False) == 1  # dry-run でも読めなかった件は隠さない
    assert mod.exit_code_for([rec(mod.RECORD_NEEDS_REVIEW), rec(mod.RECORD_READY)], apply=False) == 0
    assert mod.exit_code_for([rec("updated"), rec(mod.RECORD_NEEDS_REVIEW), rec(mod.RECORD_NOTHING_TO_DO)], apply=True) == 0
    assert mod.exit_code_for([rec("updated"), rec("stale_event")], apply=True) == 3
    assert mod.exit_code_for([rec("updated"), rec(mod.RECORD_ERROR)], apply=True) == 1


# --- 環境 -------------------------------------------------------------------------------------


def test_load_env_never_creates_new_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("AUTO_CREATE_NEW_RECORDS_ENABLED", "DATABASE_URL", "SYNC_ID_MAPPING_BACKEND", "ENABLE_ZOHO",
              "RELATION_SYNC_ENABLED", "SPREADSHEET_ROW_CREATION_ENABLED", "SPREADSHEET_ROW_CREATION_DB_KEYS",
              "DATABASE_URL_UNPOOLED", "SYNC_ID_MAPPING_NOTION_API_KEY"):
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    env = tmp_path / ".env"
    env.write_text("NOTION_API_KEY=n\nZOHO_CLIENT_ID=c\nZOHO_CLIENT_SECRET=s\nZOHO_REFRESH_TOKEN=r\nDATABASE_URL=postgres://x\n", encoding="utf-8")
    for k in ("NOTION_API_KEY", "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN"):
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k)
    mod.load_env(apply=True, env_path=env)
    assert mod.os.environ["AUTO_CREATE_NEW_RECORDS_ENABLED"] == "false"
    assert mod.os.environ["SYNC_ID_MAPPING_BACKEND"] == "notion"
    assert mod.os.environ["RELATION_SYNC_ENABLED"] == "true"


def test_zoho_v6_base_url_is_derived_from_v2_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    assert mod.zoho_v6_base_url() == "https://www.zohoapis.jp/crm/v6"


# --- Timeline の取得（偽クライアント） ---------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: dict | None = None) -> None:
        self.status_code = status
        self._body = body or {}
        self.content = b"x" if body else b""
        self.text = ""
        self.ok = 200 <= status < 300  # raise_for_error() が見る

    def json(self) -> dict:
        return self._body


def test_fetch_timeline_reads_every_page_even_if_older_entries_appear(monkeypatch: pytest.MonkeyPatch) -> None:
    """並びに頼って途中で止めない（1 件でも欠けると変更を取りこぼす）。"""
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    calls: list[str] = []

    class Client:
        def request(self, method: str, url: str, *, idempotent: bool = True) -> _Resp:
            calls.append(url)
            if "page_token=t2" in url:
                return _Resp(200, {"__timeline": [_entry("updated", "2026-09-22T10:00:00+09:00", [("f", "a", "b")])],
                                   "info": {"more_records": False, "next_page_token": None}})
            return _Resp(200, {
                "__timeline": [
                    _entry("updated", "2026-09-20T10:00:00+09:00", [("Stage", "a", "b")]),
                    _entry("updated", "2026-09-01T10:00:00+09:00", [("Stage", "z", "a")]),
                ],
                "info": {"more_records": True, "next_page_token": "t2"},
            })

    entries = mod.fetch_timeline(Client(), "Deals", "1", SINCE)
    assert len(entries) == 3
    assert calls[0] == "https://www.zohoapis.jp/crm/v6/Deals/1/__timeline?per_page=200"
    assert "page_token=t2" in calls[1]


def test_fetch_timeline_follows_page_token_and_handles_204(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)

    class Client:
        def request(self, method: str, url: str, *, idempotent: bool = True) -> _Resp:
            if "page_token=t2" in url:
                return _Resp(200, {"__timeline": [_entry("updated", "2026-09-18T10:00:00+09:00", [("f", "a", "b")])],
                                   "info": {"more_records": False, "next_page_token": None}})
            return _Resp(200, {"__timeline": [_entry("updated", "2026-09-20T10:00:00+09:00", [("g", "a", "b")])],
                               "info": {"more_records": True, "next_page_token": "t2"}})

    assert len(mod.fetch_timeline(Client(), "Deals", "1", SINCE)) == 2

    class Empty:
        def request(self, method: str, url: str, *, idempotent: bool = True) -> _Resp:
            return _Resp(204)

    assert mod.fetch_timeline(Empty(), "Deals", "1", SINCE) == []


# --- 1 レコードの計画（偽の Zoho / Notion） -----------------------------------------------------


class _Zoho:
    def __init__(self, timeline: list[dict], record: dict | None) -> None:
        self._timeline, self._record = timeline, record

    def request(self, method: str, url: str, *, idempotent: bool = True) -> _Resp:
        return _Resp(200, {"__timeline": self._timeline, "info": {"more_records": False}})

    def get_record(self, module: str, record_id: str) -> dict | None:
        return self._record


class _Notion:
    def __init__(self, page: dict | None) -> None:
        self._page = page

    def get_page(self, page_id: str) -> dict | None:
        return self._page


def _plan(zoho: _Zoho, notion: _Notion) -> "mod.RecordReplay":
    monkey_now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
    r = mod.RecordReplay(zoho_id="1", module="Deals", db_key="project", notion_key="page-1")
    mod.plan_record(r, zoho=zoho, notion_client=notion, store=None, since=SINCE, until=None, now=monkey_now)
    return r


def test_plan_record_classifies_stage_change_as_safe_when_notion_is_older(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

    timeline = [_entry("updated", "2026-09-24T17:22:57+09:00", [("Stage", "回答待ち", "口頭受注"), ("Probability", "50", "90")])]
    zoho = _Zoho(timeline, {"id": "1", "Stage": "口頭受注", "Probability": 90, "Deal_Name": "案件A", "Modified_Time": "2026-09-24T17:22:57+09:00"})
    notion = _Notion({"営業ステータス": "回答待ち", NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 10, 0, 0, tzinfo=timezone.utc)})
    r = _plan(zoho, notion)
    assert r.error is None
    assert r.outcome == mod.RECORD_READY
    by_api = {f.api_name: f for f in r.fields}
    assert by_api["Stage"].status == mod.FIELD_SAFE
    assert by_api["Stage"].notion_property == "営業ステータス"
    assert by_api["Stage"].converted_value == "口頭受注"
    assert by_api["Probability"].status == mod.FIELD_UNMAPPED  # 同期対象外
    assert [f.api_name for f in r.replayable()] == ["Stage"]


def test_plan_record_marks_ambiguous_when_notion_edited_later_and_synced_when_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

    timeline = [_entry("updated", "2026-09-24T17:22:57+09:00", [("Stage", "回答待ち", "口頭受注")])]
    zoho = _Zoho(timeline, {"id": "1", "Stage": "口頭受注", "Deal_Name": "案件A"})
    later = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
    r = _plan(zoho, _Notion({"営業ステータス": "失注", NOTION_LAST_EDITED_TIME_KEY: later}))
    assert r.fields[0].status == mod.FIELD_AMBIGUOUS
    assert r.outcome == mod.RECORD_NEEDS_REVIEW
    r2 = _plan(zoho, _Notion({"営業ステータス": "口頭受注", NOTION_LAST_EDITED_TIME_KEY: later}))
    assert r2.fields[0].status == mod.FIELD_ALREADY_SYNCED
    assert r2.outcome == mod.RECORD_NOTHING_TO_DO


def test_plan_record_does_not_replay_fields_absent_from_current_record(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

    timeline = [_entry("updated", "2026-09-24T17:22:57+09:00", [("Stage", "回答待ち", "口頭受注")])]
    zoho = _Zoho(timeline, {"id": "1", "Deal_Name": "案件A"})  # Stage が現在値に無い
    r = _plan(zoho, _Notion({"営業ステータス": "回答待ち", NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 1, tzinfo=timezone.utc)}))
    assert r.fields[0].status == mod.FIELD_MISSING_IN_RECORD
    assert r.outcome == mod.RECORD_NOTHING_TO_DO


def test_plan_record_records_errors_and_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    r = _plan(_Zoho([], None), _Notion({}))
    assert r.error == "zoho_record_not_found"
    assert r.outcome == mod.RECORD_ERROR

    class Boom(_Zoho):
        def request(self, *a, **k):
            raise RuntimeError("network")

    r = _plan(Boom([], {}), _Notion({}))
    assert r.outcome == mod.RECORD_ERROR and "network" in (r.error or "")


# --- 適用（偽 Dispatcher） --------------------------------------------------------------------


PLANNED_AT = "2026-09-27T03:00:00+00:00"
NOTION_AT = "2026-09-10T00:00:00+00:00"


class _ZohoUnchanged:
    """分類時のまま（値も Timeline も動いていない）Zoho。"""

    def __init__(self, record: dict | None = None, timeline: list[dict] | None = None) -> None:
        self._record = record if record is not None else {"Stage": "口頭受注", "Deal_Name": "別名"}
        self._timeline = timeline or []

    def get_record(self, module: str, record_id: str) -> dict | None:
        return self._record

    def request(self, method: str, url: str, *, idempotent: bool = True) -> _Resp:
        return _Resp(200, {"__timeline": self._timeline, "info": {"more_records": False}})


def _ready_record(**overrides) -> "mod.RecordReplay":
    r = mod.RecordReplay(zoho_id="1", module="Deals", db_key="project", notion_key="n",
                         notion_last_edited_at=NOTION_AT, planned_at=PLANNED_AT)
    r.fields = [mod.FieldChange(api_name="Stage", audited_at=SINCE, zoho_value="口頭受注", notion_property="営業ステータス",
                                notion_value="回答待ち", status=mod.FIELD_SAFE)]
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


def _unchanged_notion() -> "_Notion":
    return _Notion({NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 10, tzinfo=timezone.utc), "営業ステータス": "回答待ち"})


def test_apply_record_dispatches_only_replayable_fields_as_zoho_event() -> None:
    from src.db_schema.base import Tool
    from src.sync_engine.dispatcher import DispatchResult, PropertyDispatchResult

    seen = []

    class Dispatcher:
        def dispatch(self, event):
            seen.append(event)
            return DispatchResult(skipped=False, properties=(
                PropertyDispatchResult(property_name="営業ステータス", resolution=None, written_tools=frozenset({Tool.NOTION})),
            ))

    r = _ready_record()
    r.fields.append(mod.FieldChange(api_name="Deal_Name", audited_at=SINCE, zoho_value="別名", status=mod.FIELD_AMBIGUOUS))
    mod.apply_record(r, dispatcher=Dispatcher(), zoho=_ZohoUnchanged(), notion_client=_unchanged_notion(), store=None, now=datetime.now(timezone.utc))
    assert r.outcome == "updated"
    assert r.written == {"営業ステータス": ["notion"]}
    assert len(seen) == 1
    assert seen[0].source_tool is Tool.ZOHO
    assert seen[0].properties == {"営業ステータス": "口頭受注"}  # ambiguous の案件名は流れない


def test_apply_record_reports_dispatcher_skip_and_errors() -> None:
    from src.sync_engine.dispatcher import DispatchResult

    class Skipping:
        def dispatch(self, event):
            return DispatchResult(skipped=True, reason="stale_event")

    class Boom:
        def dispatch(self, event):
            raise RuntimeError("notion down")

    r = _ready_record()
    mod.apply_record(r, dispatcher=Skipping(), zoho=_ZohoUnchanged(), notion_client=_unchanged_notion(), store=None, now=datetime.now(timezone.utc))
    assert r.outcome == "stale_event"
    r = _ready_record()
    mod.apply_record(r, dispatcher=Boom(), zoho=_ZohoUnchanged(), notion_client=_unchanged_notion(), store=None, now=datetime.now(timezone.utc))
    assert r.outcome == mod.RECORD_ERROR and "notion down" in r.error


def test_apply_record_refuses_when_notion_page_was_edited_after_planning() -> None:
    """分類から書き込みまでの間に Notion が編集されていたら流さない（shirokuma-sec BLOCKER）。"""
    calls: list[str] = []

    class Dispatcher:
        def dispatch(self, event):
            calls.append("dispatched")

    now = datetime.now(timezone.utc)
    zoho = _ZohoUnchanged()
    edited = _Notion({NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 10, 0, 1, tzinfo=timezone.utc), "営業ステータス": "回答待ち"})
    r = _ready_record(); mod.apply_record(r, dispatcher=Dispatcher(), zoho=zoho, notion_client=edited, store=None, now=now)
    assert r.outcome == "notion_edited_after_plan"
    # 同じ分の編集は時刻では見えない → 流す項目の値が変わっていれば止める（ChatGPT レビュー BLOCKER）
    same_minute = _Notion({NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 10, tzinfo=timezone.utc), "営業ステータス": "失注"})
    r = _ready_record(); mod.apply_record(r, dispatcher=Dispatcher(), zoho=zoho, notion_client=same_minute, store=None, now=now)
    assert r.outcome == "notion_edited_after_plan"
    r = _ready_record(); mod.apply_record(r, dispatcher=Dispatcher(), zoho=zoho, notion_client=_Notion(None), store=None, now=now)
    assert r.outcome == "notion_edited_after_plan"  # 読めなければ安全側
    r = _ready_record(); mod.apply_record(r, dispatcher=Dispatcher(), zoho=zoho, notion_client=_Notion({}), store=None, now=now)
    assert r.outcome == "notion_edited_after_plan"  # 最終更新時刻が取れなくても安全側
    assert calls == []


def test_apply_record_refuses_when_zoho_changed_after_planning() -> None:
    """分類後に Zoho 側が更に変わっていたら、分類時の値を「今の最新」として流さない（ChatGPT レビュー BLOCKER）。"""
    from src.sync_engine.dispatcher import DispatchResult

    calls: list[str] = []

    class Dispatcher:
        def dispatch(self, event):
            calls.append("dispatched")
            return DispatchResult(skipped=False)

    now = datetime.now(timezone.utc)
    # 値が変わった
    r = _ready_record()
    mod.apply_record(r, dispatcher=Dispatcher(), zoho=_ZohoUnchanged({"Stage": "失注"}), notion_client=_unchanged_notion(), store=None, now=now)
    assert r.outcome == "zoho_edited_after_plan"
    # 値は同じだが、分類後に同じ項目が動いている（A→B→A）
    later = [_entry("updated", "2026-09-27T03:05:00+00:00", [("Stage", "x", "口頭受注")])]
    r = _ready_record()
    mod.apply_record(r, dispatcher=Dispatcher(), zoho=_ZohoUnchanged(timeline=later), notion_client=_unchanged_notion(), store=None, now=now)
    assert r.outcome == "zoho_edited_after_plan"
    # 分類後に動いたのが流さない別項目なら止めない
    other = [_entry("updated", "2026-09-27T03:05:00+00:00", [("Probability", "1", "2")])]
    r = _ready_record()
    mod.apply_record(r, dispatcher=Dispatcher(), zoho=_ZohoUnchanged(timeline=other), notion_client=_unchanged_notion(), store=None, now=now)
    assert r.outcome == "updated"
    # レコードが読めなければ安全側
    r = _ready_record()
    mod.apply_record(r, dispatcher=Dispatcher(), zoho=_ZohoUnchanged(record={}), notion_client=_unchanged_notion(), store=None, now=now)
    assert r.outcome == "zoho_edited_after_plan"
    assert calls == ["dispatched"]


def test_apply_record_reports_rejected_values_from_conflict_resolution() -> None:
    from src.db_schema.base import Tool
    from src.sync_engine.conflict_resolver import ConflictResolution, RejectedData, ResolutionAction
    from src.sync_engine.dispatcher import DispatchResult, PropertyDispatchResult

    rejected = RejectedData(record_id="n", property_name="営業ステータス", adopted_value="失注", adopted_tool=Tool.ZOHO,
                            rejected_value="アポ獲得", rejected_tool=Tool.NOTION, occurred_at=datetime.now(timezone.utc))
    resolution = ConflictResolution(action=ResolutionAction.PROPAGATE_VALUE, record_id="n", property_name="営業ステータス",
                                    resolved_value="失注", target_tools=frozenset({Tool.NOTION}), rejected=(rejected,))

    class Dispatcher:
        def dispatch(self, event):
            return DispatchResult(skipped=False, properties=(
                PropertyDispatchResult(property_name="営業ステータス", resolution=resolution, written_tools=frozenset({Tool.NOTION})),
            ))

    r = _ready_record()
    r.fields[0].zoho_value = "失注"
    mod.apply_record(r, dispatcher=Dispatcher(), zoho=_ZohoUnchanged({"Stage": "失注"}), notion_client=_unchanged_notion(), store=None, now=datetime.now(timezone.utc))
    assert r.outcome == "updated"
    assert r.rejected == [{"property": "営業ステータス", "adopted_tool": "zoho", "adopted": "失注", "rejected_tool": "notion", "rejected": "アポ獲得"}]


def test_parse_args_defaults_to_dry_run() -> None:
    args = mod.parse_args(["--since", "2026-09-15T16:00+09:00", "--trust-zoho-id", "9"])
    assert args.apply is False and args.until is None and args.trust_zoho_ids == ["9"]


# --- くま QA の指摘で追加（境界一致・以前からのズレ・収集） -----------------------------------


def test_changed_fields_includes_changes_exactly_at_since_and_until() -> None:
    until = datetime(2026, 9, 26, 17, 44, tzinfo=JST)
    entries = [
        _entry("updated", until.isoformat(), [("at_until", "a", "b")]),
        _entry("updated", SINCE.isoformat(), [("at_since", "a", "b")]),
        _entry("updated", (SINCE - timedelta(seconds=1)).isoformat(), [("before", "a", "b")]),
    ]
    assert set(mod.changed_fields_from_timeline(entries, SINCE, until)) == {"at_until", "at_since"}


def test_plan_record_flags_drift_when_notion_differs_from_zoho_old_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

    timeline = [_entry("updated", "2026-09-24T14:14:00+09:00", [("Stage", "再商談調整中", "失注")])]
    zoho = _Zoho(timeline, {"id": "1", "Stage": "失注", "Deal_Name": "案件A"})
    old_page = {NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 8, 17, tzinfo=timezone.utc)}
    r = _plan(zoho, _Notion({"営業ステータス": "アポ獲得", **old_page}))  # Zoho の変更前とも違う
    assert r.fields[0].status == mod.FIELD_SAFE and r.fields[0].drifted_before is True
    r = _plan(zoho, _Notion({"営業ステータス": "再商談調整中", **old_page}))  # 変更前と同じ＝ズレなし
    assert r.fields[0].drifted_before is False
    r = _plan(zoho, _Notion({"営業ステータス": None, **old_page}))  # Notion が空なら「ズレ」とは言わない
    assert r.fields[0].drifted_before is False


def test_collect_mapped_filters_by_mapping_and_only_ids_and_restores_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    class Zoho:
        def _headers(self) -> dict:
            return {"Authorization": "x"}

    zoho = Zoho()
    original = zoho._headers
    monkeypatch.setattr(mod.backfill, "fetch_zoho_ids_modified_since", lambda c, m, s: ["1", "2", "3"])
    monkeypatch.setattr(mod.backfill, "load_mapped_zoho_ids", lambda store, db_key: {"1": "n1", "2": "n2"})
    records = mod.collect_mapped(zoho, store=None, modules={"Deals": "project"}, since=SINCE, only_ids=None)
    assert [(r.zoho_id, r.notion_key, r.db_key) for r in records] == [("1", "n1", "project"), ("2", "n2", "project")]
    assert zoho._headers == original  # If-Modified-Since の差し込みを元に戻している（bound method は == で比べる）
    records = mod.collect_mapped(zoho, store=None, modules={"Deals": "project"}, since=SINCE, only_ids=["2", "3"])
    assert [r.zoho_id for r in records] == ["2"]  # 対応表に無い 3 は入らない

    def boom(c, m, s):
        raise RuntimeError("zoho down")

    monkeypatch.setattr(mod.backfill, "fetch_zoho_ids_modified_since", boom)
    with pytest.raises(RuntimeError):
        mod.collect_mapped(zoho, store=None, modules={"Deals": "project"}, since=SINCE, only_ids=None)
    assert zoho._headers == original  # 例外でも元に戻る


def test_resolve_notion_property_falls_back_to_label_without_mapping_table(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.sync_engine.webhook_handlers import zoho_field_transforms as zft

    monkeypatch.setitem(zft.ZOHO_LABEL_FIELD_MAPPINGS, "project", None)
    monkeypatch.delitem(zft.ZOHO_LABEL_FIELD_MAPPINGS, "project")
    assert mod.resolve_notion_property("Deals", "project", "Stage") == ("ステージ", "ステージ")


# --- ChatGPT レビューで追加（--until 後の変更・同時刻の複数変更） ---------------------------------


def test_fields_changed_after_until_and_classification() -> None:
    until = datetime(2026, 9, 26, 17, 44, tzinfo=JST)
    entries = [
        _entry("updated", "2026-09-27T09:00:00+09:00", [("Stage", "B", "C")]),  # until より後
        _entry("updated", "2026-09-20T10:00:00+09:00", [("Stage", "A", "B"), ("field1", "", "x")]),
    ]
    assert mod.fields_changed_after(entries, until) == {"Stage"}
    assert mod.fields_changed_after(entries, None) == set()
    c = mod.FieldChange(api_name="Stage", audited_at=datetime(2026, 9, 20, 10, tzinfo=JST), converted_value="C", notion_value="A")
    kw = dict(notion_last_edited_at=datetime(2026, 9, 1, tzinfo=JST), in_record=True, mapped=True, converted=True)
    assert mod.classify_field(c, changed_after_until=True, **kw) == mod.FIELD_CHANGED_AFTER_UNTIL
    assert mod.classify_field(c, changed_after_until=False, **kw) == mod.FIELD_SAFE


def test_plan_record_does_not_replay_current_value_that_changed_after_until(monkeypatch: pytest.MonkeyPatch) -> None:
    """期間内 A→B、期間後 B→C、現在値 C のとき C を流さない。"""
    monkeypatch.setenv("ZOHO_API_BASE_URL", "https://www.zohoapis.jp/crm/v2")
    from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

    timeline = [
        _entry("updated", "2026-09-27T09:00:00+09:00", [("Stage", "回答待ち", "失注")]),
        _entry("updated", "2026-09-20T10:00:00+09:00", [("Stage", "商談済み", "回答待ち")]),
    ]
    zoho = _Zoho(timeline, {"id": "1", "Stage": "失注", "Deal_Name": "案件A"})
    notion = _Notion({"営業ステータス": "商談済み", NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 1, tzinfo=timezone.utc)})
    r = mod.RecordReplay(zoho_id="1", module="Deals", db_key="project", notion_key="page-1")
    mod.plan_record(r, zoho=zoho, notion_client=notion, store=None, since=SINCE,
                    until=datetime(2026, 9, 26, 17, 44, tzinfo=JST), now=datetime(2026, 9, 27, 12, tzinfo=timezone.utc))
    assert r.fields[0].status == mod.FIELD_CHANGED_AFTER_UNTIL
    assert r.outcome == mod.RECORD_NOTHING_TO_DO


def test_changed_fields_same_second_changes_do_not_guess_order() -> None:
    entries = [
        _entry("updated", "2026-09-20T10:00:00+09:00", [("Stage", "B", "C")]),
        _entry("updated", "2026-09-20T10:00:00+09:00", [("Stage", "A", "B")]),
    ]
    found = mod.changed_fields_from_timeline(entries, SINCE)
    assert found["Stage"].order_uncertain is True and found["Stage"].display_old is None
