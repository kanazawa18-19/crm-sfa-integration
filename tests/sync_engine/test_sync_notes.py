"""判断メモの順序・非漏洩・重複防止を、外部I/Oなしで検証する。"""
from src.db_schema.base import Tool
from src.sync_engine.sync_notes import CHOICES, UNAVAILABLE, gap_notes, parse_notes, render_notes


def test_unknown_values_are_not_copied_and_sections_keep_order():
    notes = gap_notes(Tool.ZOHO, "project", {"担当メンバー": "token=private-value"})
    text = render_notes(notes)
    assert "token=private-value" not in text
    assert "[zoho:担当メンバー]" in text
    assert "送信元に値あり" in text
    assert text.index(CHOICES) < text.index(UNAVAILABLE)
    assert parse_notes(text) == notes


def test_absent_choice_does_not_claim_value_exists():
    notes = gap_notes(Tool.ZOHO, "project", {})
    assert not any("担当メンバー" in key for key in notes)
    assert any("アクション履歴" in key for key in notes)


def test_empty_choice_removes_previous_warning():
    notes = gap_notes(Tool.ZOHO, "project", {"担当メンバー": ""})
    assert notes[f"{CHOICES}|zoho:担当メンバー"] == ""


def test_missing_kintone_numeric_confidence_is_ignored():
    assert not any("確度" in key for key in gap_notes(Tool.KINTONE, "project", {}))


def test_note_failure_does_not_finish_event_and_retry_runs():
    from datetime import datetime, timezone
    import pytest
    from src.sync_engine.dispatcher import Dispatcher
    from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
    from src.sync_engine.sync_event import SyncEvent

    store = SQLiteIdMappingStore()
    store.upsert(IdMapping(notion_key="page-note-retry", db_key="chain", zoho_id="z-note"), expected_last_synced_at=None)
    calls = []

    def writer(event, mapping):
        calls.append(mapping.notion_key)
        if len(calls) == 1:
            raise RuntimeError("一時的な通信エラー")

    dispatcher = Dispatcher(store, {}, note_writer=writer)
    event = SyncEvent(Tool.ZOHO, "chain", "z-note", datetime(2026, 9, 28, tzinfo=timezone.utc),
                      sync_notes=gap_notes(Tool.ZOHO, "chain", {}))
    with pytest.raises(RuntimeError):
        dispatcher.dispatch(event)
    assert store.get("page-note-retry").last_synced_at is None
    assert not dispatcher.dispatch(event).skipped
    assert len(calls) == 2
    assert dispatcher.dispatch(event).reason == "stale_event"
    assert len(calls) == 2


def test_business_names_remain_visible_without_user_id_or_email():
    notes = gap_notes(Tool.ZOHO, "project", {"担当メンバー": {"name": "担当サンプル", "id": "private-id", "email": "private@example.com"}})
    text = render_notes(notes)
    assert "担当サンプル" in text
    assert "private-id" not in text and "private@example.com" not in text


def test_partial_unrelated_event_preserves_previous_business_value():
    from src.sync_engine.sync_notes import merge_notes
    first=gap_notes(Tool.ZOHO,'chain',{'その他ブランド':'保存したい業務値'})
    second=gap_notes(Tool.ZOHO,'chain',{'電話':'test'})
    merged=merge_notes(first,second)
    assert '保存したい業務値' in render_notes(merged)
    cleared=merge_notes(merged,gap_notes(Tool.ZOHO,'chain',{'その他ブランド':''}))
    assert '保存したい業務値' not in render_notes(cleared)
