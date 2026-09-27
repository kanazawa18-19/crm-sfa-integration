"""kintone の更新再送（replay_kintone_missed_updates.py）と作成漏れ回収（backfill_kintone_missed_records.py）のテスト。

本番 API は叩かない。流す項目の絞り込み・書く直前の読み直し・終了コード・作成の流し方を固定する。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.db_schema.base import Tool
from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


inv_mod = _load("inventory_kintone_missed_updates")
replay = _load("replay_kintone_missed_updates")
backfill_k = _load("backfill_kintone_missed_records")

UTC = timezone.utc
PLANNED_EDIT = datetime(2026, 9, 10, 1, 0, tzinfo=UTC)


def _inv(fields: list[tuple[str, object, object, str]], **kw) -> "inv_mod.RecordInventory":
    r = inv_mod.RecordInventory(
        db_key="client_master", kintone_id="1", updated_at="2026-09-15T00:25:00+00:00",
        notion_key="page-1", notion_last_edited_at=PLANNED_EDIT.isoformat(), **kw,
    )
    r.fields = [inv_mod.FieldDiff(*f) for f in fields]
    return r


# --- 純粋な部分 ---------------------------------------------------------------------------------


def test_safe_properties_takes_only_safe_fields() -> None:
    r = _inv([("住所", "新", "旧", "safe"), ("電話", "x", "y", "ambiguous"), ("名前", "a", " a", "whitespace_only"), ("備考", "b", "b", "already_synced")])
    assert replay.safe_properties(r) == {"住所": "新"}


def test_exit_code_apply_fails_on_unwritten_ready_but_not_on_review() -> None:
    def rec(outcome: str):
        return SimpleNamespace(outcome=outcome)

    assert replay.exit_code_for([rec("updated"), rec("needs_review"), rec("not_mapped")], apply=True) == 0
    assert replay.exit_code_for([rec("ready")], apply=True) == 3  # 分類したのに流していない
    assert replay.exit_code_for([rec("notion_edited_after_plan")], apply=True) == 3
    assert replay.exit_code_for([rec("ready")], apply=False) == 0
    assert replay.exit_code_for([rec("error")], apply=False) == 1


def test_values_equal_or_both_empty_treats_none_blank_and_empty_list_alike() -> None:
    assert replay.values_equal_or_both_empty(None, [])
    assert replay.values_equal_or_both_empty("", None)
    assert not replay.values_equal_or_both_empty("a", "b")


# --- 書く直前の読み直し -------------------------------------------------------------------------


class _Notion:
    def __init__(self, page):
        self.page = page

    def get_page(self, key):
        return self.page


def test_notion_unchanged_guard() -> None:
    r = _inv([("住所", "新", "旧", "safe")])
    ok = _Notion({NOTION_LAST_EDITED_TIME_KEY: PLANNED_EDIT, "住所": "旧"})
    assert replay.notion_page_unchanged_since_plan(r, ok)
    edited = _Notion({NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 20, tzinfo=UTC), "住所": "旧"})
    assert not replay.notion_page_unchanged_since_plan(r, edited)
    value_moved = _Notion({NOTION_LAST_EDITED_TIME_KEY: PLANNED_EDIT, "住所": "別の値"})
    assert not replay.notion_page_unchanged_since_plan(r, value_moved)
    assert not replay.notion_page_unchanged_since_plan(r, _Notion(None))


def test_apply_skips_when_kintone_record_changed_after_plan(monkeypatch) -> None:
    r = _inv([("住所", "新", "旧", "safe")])
    monkeypatch.setattr(replay, "fetch_kintone_record", lambda db, kid: {"更新日時": {"value": "2026-09-16T00:00:00Z"}})
    dispatched = []
    disp = SimpleNamespace(dispatch=lambda e: dispatched.append(e))
    replay.apply_record(r, app_id="9", dispatcher=disp, notion_client=_Notion(None), store=None, now=datetime.now(UTC))
    assert r.outcome == "kintone_edited_after_plan"
    assert dispatched == []


def test_apply_skips_when_notion_edited_after_plan(monkeypatch) -> None:
    r = _inv([("住所", "新", "旧", "safe")])
    monkeypatch.setattr(replay, "fetch_kintone_record", lambda db, kid: {"更新日時": {"value": "2026-09-15T00:25:00Z"}})
    edited = _Notion({NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 20, tzinfo=UTC), "住所": "旧"})
    dispatched = []
    disp = SimpleNamespace(dispatch=lambda e: dispatched.append(e))
    replay.apply_record(r, app_id="9", dispatcher=disp, notion_client=edited, store=None, now=datetime.now(UTC))
    assert r.outcome == "notion_edited_after_plan"
    assert dispatched == []


# --- 作成漏れの回収 -----------------------------------------------------------------------------


class _Store:
    def __init__(self, mapped: set[str]):
        self.mapped = mapped

    def find_by_external_id(self, tool, ext_id, db_key=None):
        assert tool is Tool.KINTONE
        return SimpleNamespace(notion_key="n-" + ext_id) if ext_id in self.mapped else None


class _Dispatcher:
    def __init__(self, reasons: dict[str, str | None]):
        self.reasons, self.events = reasons, []

    def dispatch(self, event):
        self.events.append(event)
        reason = self.reasons[event.external_id]
        if reason == "boom":
            raise RuntimeError("boom")
        return SimpleNamespace(skipped=reason is not None, reason=reason)


def test_backfill_apply_sends_empty_event_and_counts_outcomes() -> None:
    items = [backfill_k.Missed("client_master", k) for k in ("1", "2", "3", "4")]
    disp = _Dispatcher({"1": None, "2": "new_record_missing_required_properties", "3": "boom"})
    counts = backfill_k.apply_all(disp, items, store=_Store({"4"}), sleep_seconds=0)
    assert counts == {"created": 1, "new_record_missing_required_properties": 1, "error": 1, "mapped_meanwhile": 1}
    assert [e.external_id for e in disp.events] == ["1", "2", "3"]  # 対応表に載っていた 4 は流さない
    assert all(e.source_tool is Tool.KINTONE and e.properties == {} for e in disp.events)


def test_backfill_exit_codes() -> None:
    assert backfill_k.exit_code_for({"created": 3, "mapped_meanwhile": 1}) == 0
    assert backfill_k.exit_code_for({"created": 1, "error": 1}) == 1
    assert backfill_k.exit_code_for({"created": 1, "new_record_missing_required_properties": 1}) == 3


# --- apply_record の正常系・分岐 ---------------------------------------------------------------


def _apply_setup(monkeypatch, *, event_props, kintone_updated="2026-09-15T00:25:00Z"):
    import src.sync_engine.webhook_handlers.kintone_webhook as kw
    from src.sync_engine.sync_event import SyncEvent

    monkeypatch.setattr(replay, "fetch_kintone_record", lambda db, kid: {"更新日時": {"value": kintone_updated}})
    old = datetime(2026, 9, 15, 0, 25, tzinfo=UTC)
    monkeypatch.setattr(kw, "kintone_payload_to_sync_event", lambda *a, **k: SyncEvent(
        source_tool=Tool.KINTONE, db_key="client_master", external_id="1", occurred_at=old, properties=dict(event_props)))
    return _Notion({NOTION_LAST_EDITED_TIME_KEY: PLANNED_EDIT, "住所": "旧"})


def _prop_result(name, tools):
    return SimpleNamespace(property_name=name, written_tools=[SimpleNamespace(value=t) for t in tools], resolution=None)


def test_apply_dispatches_only_safe_props_with_now(monkeypatch) -> None:
    r = _inv([("住所", "新", "旧", "safe"), ("電話", "x", "y", "ambiguous")])
    notion = _apply_setup(monkeypatch, event_props={"住所": "新", "電話": "x", "備考": "z"})
    seen = []
    disp = SimpleNamespace(dispatch=lambda e: seen.append(e) or SimpleNamespace(skipped=False, reason=None, properties=[_prop_result("住所", ["notion", "zoho"])]))
    now = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)
    replay.apply_record(r, app_id="9", dispatcher=disp, notion_client=notion, store=None, now=now)
    assert r.outcome == "updated"
    assert seen[0].properties == {"住所": "新"}  # ambiguous と対象外の項目は流さない
    assert seen[0].occurred_at == now  # 古い更新時刻のままだと stale_event になる
    assert r.written == {"住所": ["notion", "zoho"]}


def test_apply_outcomes_for_skip_error_and_nothing_converted(monkeypatch) -> None:
    r = _inv([("住所", "新", "旧", "safe")])
    notion = _apply_setup(monkeypatch, event_props={"住所": "新"})
    skipped = SimpleNamespace(dispatch=lambda e: SimpleNamespace(skipped=True, reason="stale_event", properties=[]))
    replay.apply_record(r, app_id="9", dispatcher=skipped, notion_client=notion, store=None, now=datetime.now(UTC))
    assert r.outcome == "stale_event"

    def boom(e):
        raise RuntimeError("x")

    r2 = _inv([("住所", "新", "旧", "safe")])
    replay.apply_record(r2, app_id="9", dispatcher=SimpleNamespace(dispatch=boom), notion_client=notion, store=None, now=datetime.now(UTC))
    assert r2.outcome == "error" and "RuntimeError" in r2.error

    r3 = _inv([("住所", "新", "旧", "safe")])
    notion3 = _apply_setup(monkeypatch, event_props={"別項目": "v"})
    replay.apply_record(r3, app_id="9", dispatcher=skipped, notion_client=notion3, store=None, now=datetime.now(UTC))
    assert r3.outcome == "nothing_converted"


def test_apply_skips_when_kintone_record_gone(monkeypatch) -> None:
    r = _inv([("住所", "新", "旧", "safe")])
    monkeypatch.setattr(replay, "fetch_kintone_record", lambda db, kid: None)
    replay.apply_record(r, app_id="9", dispatcher=None, notion_client=_Notion(None), store=None, now=datetime.now(UTC))
    assert r.outcome == "kintone_edited_after_plan"


def test_notion_guard_fails_safe_when_plan_data_missing() -> None:
    page = _Notion({NOTION_LAST_EDITED_TIME_KEY: PLANNED_EDIT, "住所": "旧"})
    r = _inv([("住所", "新", "旧", "safe")])
    r.notion_last_edited_at = None
    assert not replay.notion_page_unchanged_since_plan(r, page)
    r2 = _inv([("住所", "新", "旧", "safe")])
    r2.notion_key = None
    assert not replay.notion_page_unchanged_since_plan(r2, page)
    r3 = _inv([("住所", "新", "旧", "safe")])
    assert not replay.notion_page_unchanged_since_plan(r3, _Notion({NOTION_LAST_EDITED_TIME_KEY: "not-a-datetime", "住所": "旧"}))


def test_limit_skipped_records_do_not_fail_exit_code() -> None:
    assert replay.exit_code_for([SimpleNamespace(outcome="updated"), SimpleNamespace(outcome="not_applied_by_limit")], apply=True) == 0
