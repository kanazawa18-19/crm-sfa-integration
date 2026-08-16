from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from src.audit_log import recorder
from src.audit_log.actor_context import set_actor


class _RecordedCall:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs


@pytest.fixture
def recorded_calls(monkeypatch: pytest.MonkeyPatch) -> list[_RecordedCall]:
    calls: list[_RecordedCall] = []

    def _fake_insert_audit_log(**kwargs: Any) -> None:
        calls.append(_RecordedCall(**kwargs))

    monkeypatch.setattr(recorder.db, "insert_audit_log", _fake_insert_audit_log)
    return calls


# --- create ------------------------------------------------------------------------------


def test_create_records_all_properties_with_before_none(recorded_calls: list[_RecordedCall]) -> None:
    with set_actor("kintone_webhook"):
        recorder.record_notion_write(
            db_key="client_master",
            notion_page_id="page-1",
            action="create",
            before=None,
            after={"取引先名": "株式会社サンプル", "顧客種別": "ホテル・旅館"},
        )

    assert len(recorded_calls) == 1
    call = recorded_calls[0]
    assert call.kwargs["db_key"] == "client_master"
    assert call.kwargs["notion_page_id"] == "page-1"
    assert call.kwargs["action"] == "create"
    assert call.kwargs["changed_fields"] == {
        "取引先名": {"before": None, "after": "株式会社サンプル"},
        "顧客種別": {"before": None, "after": "ホテル・旅館"},
    }
    assert call.kwargs["actor_source"] == "kintone_webhook"
    assert call.kwargs["actor_label"] is None


# --- update ------------------------------------------------------------------------------


def test_update_records_only_changed_properties(recorded_calls: list[_RecordedCall]) -> None:
    with set_actor("gmail_sync"):
        recorder.record_notion_write(
            db_key="contact",
            notion_page_id="page-2",
            action="update",
            before={"最終メール日時": "2026-08-01T00:00:00+00:00", "名前": "山田太郎"},
            after={"最終メール日時": "2026-08-16T09:00:00+00:00", "名前": "山田太郎"},
        )

    assert len(recorded_calls) == 1
    changed = recorded_calls[0].kwargs["changed_fields"]
    assert changed == {
        "最終メール日時": {"before": "2026-08-01T00:00:00+00:00", "after": "2026-08-16T09:00:00+00:00"}
    }


def test_update_skips_when_before_is_none(recorded_calls: list[_RecordedCall]) -> None:
    """`before`（更新前値の取得結果）がNoneの場合は「取得失敗」を意味し、誤った内容を
    記録するより記録自体をスキップする(HttpNotionClient._fetch_current_values_for_auditの
    docstring参照)。"""
    recorder.record_notion_write(
        db_key="contact",
        notion_page_id="page-3",
        action="update",
        before=None,
        after={"名前": "新しい名前"},
    )

    assert recorded_calls == []


def test_update_skips_when_nothing_actually_changed(recorded_calls: list[_RecordedCall]) -> None:
    recorder.record_notion_write(
        db_key="contact",
        notion_page_id="page-4",
        action="update",
        before={"名前": "山田太郎"},
        after={"名前": "山田太郎"},
    )

    assert recorded_calls == []


def test_unsupported_action_does_not_raise_and_is_not_recorded(
    recorded_calls: list[_RecordedCall],
) -> None:
    """`action`のバリデーション失敗を含め、本関数は例外を外へ送出しない
    （呼び出し元のcreate_page/update_pageから見て、Notion書き込み自体は成功したように
    扱われるべきという設計方針。obasan-qualityレビューWARN対応、2026-08-17）。"""
    recorder.record_notion_write(
        db_key="contact", notion_page_id="page-5", action="delete", before=None, after={}
    )

    assert recorded_calls == []


# --- actor -------------------------------------------------------------------------------


def test_records_unknown_actor_when_not_set(recorded_calls: list[_RecordedCall]) -> None:
    recorder.record_notion_write(
        db_key="contact",
        notion_page_id="page-6",
        action="create",
        before=None,
        after={"名前": "山田太郎"},
    )

    assert recorded_calls[0].kwargs["actor_source"] == "unknown"


# --- _to_jsonable --------------------------------------------------------------------------


def test_to_jsonable_passes_through_primitives() -> None:
    assert recorder._to_jsonable("text") == "text"
    assert recorder._to_jsonable(123) == 123
    assert recorder._to_jsonable(1.5) == 1.5
    assert recorder._to_jsonable(True) is True
    assert recorder._to_jsonable(None) is None


def test_to_jsonable_converts_datetime_and_date_to_isoformat() -> None:
    dt = datetime(2026, 8, 16, 9, 0, 0, tzinfo=timezone.utc)
    assert recorder._to_jsonable(dt) == dt.isoformat()
    d = date(2026, 8, 16)
    assert recorder._to_jsonable(d) == d.isoformat()


def test_to_jsonable_converts_list_items_recursively() -> None:
    dt = datetime(2026, 8, 16, 9, 0, 0, tzinfo=timezone.utc)
    assert recorder._to_jsonable(["a", dt, 1]) == ["a", dt.isoformat(), 1]


def test_to_jsonable_falls_back_to_str_for_unknown_types() -> None:
    class Custom:
        def __str__(self) -> str:
            return "custom-value"

    assert recorder._to_jsonable(Custom()) == "custom-value"


# --- db failure isolation ------------------------------------------------------------------


def test_db_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(**kwargs: Any) -> None:
        raise ValueError("DATABASE_URL is not set")

    monkeypatch.setattr(recorder.db, "insert_audit_log", _raise)

    # 例外を送出しないこと（監査ログの失敗が呼び出し元のNotion書き込みを止めてはならない）。
    recorder.record_notion_write(
        db_key="contact",
        notion_page_id="page-7",
        action="create",
        before=None,
        after={"名前": "山田太郎"},
    )
