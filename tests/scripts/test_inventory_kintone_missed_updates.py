"""kintone 側の更新漏れ洗い出し（scripts/inventory_kintone_missed_updates.py）の純粋な部分のテスト。

本番 API は叩かない。分類ロジック・時刻の扱い・1 件の失敗が全体を止めないことを固定する。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.sync_engine.clients._notion_keys import NOTION_LAST_EDITED_TIME_KEY

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_spec = importlib.util.spec_from_file_location("inventory_kintone_missed_updates", _SCRIPTS / "inventory_kintone_missed_updates.py")
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = mod  # dataclass が `from __future__ import annotations` の型を解決するのに要る
_spec.loader.exec_module(mod)

UTC = timezone.utc
T = datetime(2026, 9, 15, 0, 25, 30, tzinfo=UTC)  # kintone の更新時刻（30 秒付き）


# --- 時刻 ------------------------------------------------------------------------------------


def test_parse_kintone_time_reads_z_suffix_as_utc() -> None:
    assert mod.parse_kintone_time("2026-09-15T00:25:00Z") == datetime(2026, 9, 15, 0, 25, tzinfo=UTC)


def test_kintone_query_time_converts_to_utc_plus0000() -> None:
    jst = datetime(2026, 8, 31, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    assert mod.kintone_query_time(jst) == "2026-08-30T15:00:00+0000"


def test_fmt_jst_shows_japan_time_and_tolerates_missing() -> None:
    assert mod.fmt_jst("2026-09-25T09:47:00+00:00") == "09-25 18:47"
    assert mod.fmt_jst(None) == "-"


def test_kintone_id_arg_accepts_digits_only() -> None:
    assert mod.kintone_id_arg("62388") == "62388"
    with pytest.raises(Exception):
        mod.kintone_id_arg("62388 or 1=1")


# --- 項目の分類 --------------------------------------------------------------------------------


def test_same_value_is_already_synced_and_empty_pair_counts_as_same() -> None:
    assert mod.classify_field("北海道", "北海道", notion_last_edited_at=None, kintone_updated_at=T) == mod.FIELD_ALREADY_SYNCED
    assert mod.classify_field(None, "", notion_last_edited_at=None, kintone_updated_at=T) == mod.FIELD_ALREADY_SYNCED


def test_leading_or_trailing_whitespace_only_is_not_a_real_difference() -> None:
    assert mod.classify_field("愛知県", " 愛知県", notion_last_edited_at=T - timedelta(days=1), kintone_updated_at=T) == mod.FIELD_WHITESPACE_ONLY


def test_safe_only_when_notion_untouched_before_the_minute_of_kintone_update() -> None:
    minute = T.replace(second=0, microsecond=0)
    assert mod.classify_field("a", "b", notion_last_edited_at=minute - timedelta(minutes=1), kintone_updated_at=T) == mod.FIELD_SAFE
    # 同じ分は安全と言い切れない（Notion の last_edited_time は分単位）
    assert mod.classify_field("a", "b", notion_last_edited_at=minute, kintone_updated_at=T) == mod.FIELD_AMBIGUOUS
    assert mod.classify_field("a", "b", notion_last_edited_at=minute + timedelta(minutes=5), kintone_updated_at=T) == mod.FIELD_AMBIGUOUS


def test_missing_notion_last_edited_time_falls_back_to_ambiguous() -> None:
    assert mod.classify_field("a", "b", notion_last_edited_at=None, kintone_updated_at=T) == mod.FIELD_AMBIGUOUS


# --- レコードの分類 -----------------------------------------------------------------------------


def _inv(**kw) -> "mod.RecordInventory":
    return mod.RecordInventory(db_key="client_master", kintone_id="1", **kw)


def test_record_outcome_priority() -> None:
    assert mod.record_outcome(_inv(error="boom")) == mod.RECORD_ERROR
    assert mod.record_outcome(_inv(outcome=mod.RECORD_NOT_MAPPED)) == mod.RECORD_NOT_MAPPED
    inv = _inv(fields=[mod.FieldDiff("TEL", "1", "2", mod.FIELD_AMBIGUOUS), mod.FieldDiff("住所", "a", "b", mod.FIELD_SAFE)])
    assert mod.record_outcome(inv) == mod.RECORD_READY
    inv = _inv(fields=[mod.FieldDiff("TEL", "1", "2", mod.FIELD_AMBIGUOUS)])
    assert mod.record_outcome(inv) == mod.RECORD_NEEDS_REVIEW
    inv = _inv(fields=[mod.FieldDiff("TEL", "1", "1", mod.FIELD_ALREADY_SYNCED), mod.FieldDiff("都道府県", "a", " a", mod.FIELD_WHITESPACE_ONLY)])
    assert mod.record_outcome(inv) == mod.RECORD_NOTHING_TO_DO
    assert mod.record_outcome(_inv()) == mod.RECORD_NOTHING_TO_DO


def test_label_of_prefers_customer_name_and_ignores_malformed_fields() -> None:
    assert mod.label_of({"顧客名": {"value": "genkan"}, "案件名": {"value": "x"}}) == "genkan"
    assert mod.label_of({"顧客名": "壊れた形", "案件名": {"value": "案件"}}) == "案件"
    assert mod.label_of({}) == ""


# --- plan_record（I/O を偽物に差し替え） ------------------------------------------------------------


class _Store:
    def __init__(self, mapping):
        self.mapping = mapping

    def find_by_external_id(self, tool, external_id, *, db_key):
        return self.mapping


class _Notion:
    def __init__(self, page):
        self.page = page

    def get_page(self, page_id):
        return self.page


def _raw(created: str, updated: str, **fields) -> dict:
    rec = {"$id": {"value": "62181"}, "作成日時": {"value": created}, "更新日時": {"value": updated},
           "更新者": {"value": {"code": "u", "name": "上村"}}, "顧客名": {"value": "札幌インター自動車学校"}}
    rec.update({k: {"value": v} for k, v in fields.items()})
    return rec


@pytest.fixture
def since() -> datetime:
    return datetime(2026, 8, 30, 15, 0, tzinfo=UTC)


def test_plan_record_marks_not_mapped_without_touching_notion(since) -> None:
    inv = _inv()
    mod.plan_record(inv, _raw("2026-09-16T01:00:00Z", "2026-09-16T01:00:00Z"), app_id="15", since=since, store=_Store(None), notion_client=None)
    assert inv.outcome == mod.RECORD_NOT_MAPPED
    assert inv.created_in_window is True
    assert inv.label == "札幌インター自動車学校" and inv.updater == "上村"


def test_plan_record_compares_records_created_in_window_too(since, monkeypatch) -> None:
    """作成直後の編集も同じ不具合で落ちうるので、作成時刻で除外しない（shirokuma-sec BLOCKER）。"""
    event = SimpleNamespace(properties={"TEL": "0118752211"})
    monkeypatch.setattr("src.sync_engine.webhook_handlers.kintone_webhook.kintone_payload_to_sync_event", lambda *a, **k: event)
    page = {"TEL": "0113752155", NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 9, 16, 1, 0, tzinfo=UTC)}
    inv = _inv()
    mod.plan_record(inv, _raw("2026-09-16T01:00:00Z", "2026-09-16T01:00:30Z"), app_id="15", since=since,
                    store=_Store(SimpleNamespace(notion_key="page-1")), notion_client=_Notion(page))
    assert inv.created_in_window is True
    assert [f.status for f in inv.fields] == [mod.FIELD_AMBIGUOUS]  # 同じ分に Notion が書かれている
    assert inv.outcome == mod.RECORD_NEEDS_REVIEW


def test_plan_record_safe_when_notion_older(since, monkeypatch) -> None:
    event = SimpleNamespace(properties={"TEL": "0118752211", "住所": "札幌市"})
    monkeypatch.setattr("src.sync_engine.webhook_handlers.kintone_webhook.kintone_payload_to_sync_event", lambda *a, **k: event)
    page = {"TEL": "0113752155", "住所": "札幌市", NOTION_LAST_EDITED_TIME_KEY: datetime(2026, 8, 28, 10, 14, tzinfo=UTC)}
    inv = _inv()
    mod.plan_record(inv, _raw("2026-08-01T00:00:00Z", "2026-09-25T09:47:00Z"), app_id="15", since=since,
                    store=_Store(SimpleNamespace(notion_key="page-1")), notion_client=_Notion(page))
    assert inv.created_in_window is False
    assert {f.notion_property: f.status for f in inv.fields} == {"TEL": mod.FIELD_SAFE, "住所": mod.FIELD_ALREADY_SYNCED}
    assert inv.outcome == mod.RECORD_READY
    assert inv.notion_last_edited_at == "2026-08-28T10:14:00+00:00"


def test_plan_record_keeps_going_after_a_broken_record(since) -> None:
    inv = _inv()
    mod.plan_record(inv, {"$id": {"value": "1"}}, app_id="15", since=since, store=_Store(None), notion_client=None)  # 作成日時が無い
    assert inv.outcome == mod.RECORD_ERROR
    assert "KeyError" in (inv.error or "")


def test_plan_record_notion_page_missing_is_an_error(since, monkeypatch) -> None:
    monkeypatch.setattr("src.sync_engine.webhook_handlers.kintone_webhook.kintone_payload_to_sync_event", lambda *a, **k: SimpleNamespace(properties={}))
    inv = _inv()
    mod.plan_record(inv, _raw("2026-08-01T00:00:00Z", "2026-09-25T09:47:00Z"), app_id="15", since=since,
                    store=_Store(SimpleNamespace(notion_key="page-1")), notion_client=_Notion(None))
    assert inv.outcome == mod.RECORD_ERROR and inv.error == "notion_page_not_found"
