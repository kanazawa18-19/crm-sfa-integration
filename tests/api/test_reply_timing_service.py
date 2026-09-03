"""返信傾向を画面向けのdictにする層の検証（2026-09-03）。"""

from __future__ import annotations

from datetime import datetime, timezone

from src.api import reply_timing_service


def _row(page_id: str, direction: str, at: datetime, email: str = "a@example.com") -> dict:
    return {
        "contactPageId": page_id,
        "contactEmail": email,
        "direction": direction,
        "sentAt": at,
    }


def _utc(day: int, hour: int) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


def test_build_for_contact_page_ids_returns_empty_without_page_ids(monkeypatch) -> None:
    def fail_fetch(page_ids):
        raise AssertionError("ページIDが空ならDBを引かないこと")

    monkeypatch.setattr(reply_timing_service.db, "fetch_email_events_by_contact_page_ids", fail_fetch)

    assert reply_timing_service.build_for_contact_page_ids([]) == {}


def test_build_for_contact_page_ids_keys_by_page_id(monkeypatch) -> None:
    rows = [
        _row("cnt-1", "outbound", _utc(1, 1)),
        _row("cnt-1", "inbound", _utc(1, 4)),
        _row("cnt-2", "outbound", _utc(1, 1)),
    ]
    monkeypatch.setattr(
        reply_timing_service.db, "fetch_email_events_by_contact_page_ids", lambda ids: rows
    )

    result = reply_timing_service.build_for_contact_page_ids(["cnt-1", "cnt-2"])

    assert set(result) == {"cnt-1", "cnt-2"}
    assert result["cnt-1"]["median_lag_label"] == "3時間"
    assert result["cnt-1"]["sample_size"] == 1
    # 送信しかない連絡先は返信ラグ0件だが、キー自体は返す。
    assert result["cnt-2"]["sample_size"] == 0
    assert result["cnt-2"]["median_lag_label"] == "—"


def test_build_for_contact_page_ids_merges_multiple_addresses_of_one_contact(monkeypatch) -> None:
    """同じ人が2つのアドレスを使っていても1人として数える。"""
    rows = [
        _row("cnt-1", "outbound", _utc(1, 1), email="a@example.com"),
        _row("cnt-1", "inbound", _utc(1, 2), email="a@example.com"),
        _row("cnt-1", "outbound", _utc(2, 1), email="b@example.com"),
        _row("cnt-1", "inbound", _utc(2, 2), email="b@example.com"),
    ]
    monkeypatch.setattr(
        reply_timing_service.db, "fetch_email_events_by_contact_page_ids", lambda ids: rows
    )

    result = reply_timing_service.build_for_contact_page_ids(["cnt-1"])

    assert result["cnt-1"]["sample_size"] == 2
    assert result["cnt-1"]["inbound_count"] == 2


def test_build_for_contact_page_ids_returns_empty_when_db_read_fails(monkeypatch) -> None:
    """EmailLogが読めなくても360ビュー全体を落とさない。"""

    def boom(page_ids):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(reply_timing_service.db, "fetch_email_events_by_contact_page_ids", boom)

    assert reply_timing_service.build_for_contact_page_ids(["cnt-1"]) == {}


def test_note_states_sample_counts(monkeypatch) -> None:
    rows = [
        _row("cnt-1", "outbound", _utc(1, 1)),
        _row("cnt-1", "inbound", _utc(1, 2)),
    ]
    monkeypatch.setattr(
        reply_timing_service.db, "fetch_email_events_by_contact_page_ids", lambda ids: rows
    )

    note = reply_timing_service.build_for_contact_page_ids(["cnt-1"])["cnt-1"]["note"]

    assert "返信1件" in note
    assert "参考値" in note


def test_note_explains_when_only_inbound_exists(monkeypatch) -> None:
    rows = [_row("cnt-1", "inbound", _utc(1, 2))]
    monkeypatch.setattr(
        reply_timing_service.db, "fetch_email_events_by_contact_page_ids", lambda ids: rows
    )

    result = reply_timing_service.build_for_contact_page_ids(["cnt-1"])["cnt-1"]

    assert result["sample_size"] == 0
    assert result["timing"]["sample_size"] == 1
    assert "返信ラグは出せません" in result["note"]


def test_timing_dict_carries_both_top_and_full_buckets(monkeypatch) -> None:
    rows = [_row("cnt-1", "inbound", _utc(1, 4))]  # JST 13時
    monkeypatch.setattr(
        reply_timing_service.db, "fetch_email_events_by_contact_page_ids", lambda ids: rows
    )

    timing = reply_timing_service.build_for_contact_page_ids(["cnt-1"])["cnt-1"]["timing"]

    assert timing["top_buckets"] == [{"label": "12-15時", "count": 1}]
    assert len(timing["buckets"]) == 8
    assert len(timing["weekday_counts"]) == 7
