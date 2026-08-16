from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.email_reminders import reminder_check
from src.email_reminders.slack_notify import SlackDeliveryError


def _row(
    *,
    id_: str = "log1",
    contact_page_id: str = "contact1",
    contact_email: str = "lead@client.example.com",
    rep_email: str = "rep@cnctor.jp",
    hours_ago: float = 10,
    subject: str | None = "件名",
) -> dict:
    return {
        "id": id_,
        "contactPageId": contact_page_id,
        "contactEmail": contact_email,
        "repEmail": rep_email,
        "subject": subject,
        "sentAt": datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    }


def test_run_reminder_check_skips_entirely_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(reminder_check.db, "get_reminder_settings", lambda: (False, [3, 6, 24]))

    def fail_find_candidates():
        raise AssertionError("find_latest_inbound_awaiting_reply should not be called when disabled")

    monkeypatch.setattr(reminder_check.db, "find_latest_inbound_awaiting_reply", fail_find_candidates)

    result = reminder_check.run_reminder_check()

    assert result == {"disabled": 1}


def test_run_reminder_check_selects_the_largest_crossed_threshold(monkeypatch) -> None:
    # 経過10時間、閾値[3,6,24]が有効 -> 3と6はクロス済み、24は未クロス。最大の6が選ばれる。
    monkeypatch.setattr(reminder_check.db, "get_reminder_settings", lambda: (True, [3, 6, 24]))
    monkeypatch.setattr(
        reminder_check.db, "find_latest_inbound_awaiting_reply", lambda: [_row(hours_ago=10)]
    )

    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        reminder_check.db,
        "record_reminder_sent",
        lambda email_log_id, threshold: recorded.append((email_log_id, threshold)) or True,
    )
    sent_calls: list[dict] = []
    monkeypatch.setattr(
        reminder_check.slack_notify, "send_reminder_dm", lambda **kwargs: sent_calls.append(kwargs)
    )

    result = reminder_check.run_reminder_check()

    assert sent_calls[0]["rep_email"] == "rep@cnctor.jp"
    assert recorded == [("log1", 6)]
    assert result == {"eligible": 1, "sent": 1, "failed": 0}


def test_run_reminder_check_skips_when_no_threshold_crossed_yet(monkeypatch) -> None:
    monkeypatch.setattr(reminder_check.db, "get_reminder_settings", lambda: (True, [24, 48]))
    monkeypatch.setattr(
        reminder_check.db, "find_latest_inbound_awaiting_reply", lambda: [_row(hours_ago=2)]
    )

    def fail_record(*args, **kwargs):
        raise AssertionError("record_reminder_sent should not be called when no threshold has crossed")

    monkeypatch.setattr(reminder_check.db, "record_reminder_sent", fail_record)

    def fail_send(*args, **kwargs):
        raise AssertionError("send_reminder_dm should not be called when no threshold has crossed")

    monkeypatch.setattr(reminder_check.slack_notify, "send_reminder_dm", fail_send)

    result = reminder_check.run_reminder_check()

    assert result == {"eligible": 1, "sent": 0, "failed": 0}


def test_run_reminder_check_does_not_send_when_record_reminder_sent_reports_duplicate(
    monkeypatch,
) -> None:
    """`record_reminder_sent()`がFalse(=`ON CONFLICT DO NOTHING`で新規挿入できなかった、
    つまり既に他の実行が処理済み)を返す場合はSlack送信をスキップする(二重送信防止、
    shirokuma-secレビューWARN対応、2026-08-16)。"""
    monkeypatch.setattr(reminder_check.db, "get_reminder_settings", lambda: (True, [3, 6]))
    monkeypatch.setattr(
        reminder_check.db, "find_latest_inbound_awaiting_reply", lambda: [_row(hours_ago=10)]
    )
    monkeypatch.setattr(reminder_check.db, "record_reminder_sent", lambda email_log_id, threshold: False)

    def fail_send(*args, **kwargs):
        raise AssertionError("send_reminder_dm should not be called when record_reminder_sent reports duplicate")

    monkeypatch.setattr(reminder_check.slack_notify, "send_reminder_dm", fail_send)

    result = reminder_check.run_reminder_check()

    assert result == {"eligible": 1, "sent": 0, "failed": 0}


def test_run_reminder_check_records_before_sending(monkeypatch) -> None:
    """レースコンディション対策: 送信記録(record_reminder_sent)を先にアトミックに試みてから
    Slack送信する順序になっていることを確認する(shirokuma-secレビューWARN対応、2026-08-16)。"""
    monkeypatch.setattr(reminder_check.db, "get_reminder_settings", lambda: (True, [3]))
    monkeypatch.setattr(
        reminder_check.db, "find_latest_inbound_awaiting_reply", lambda: [_row(hours_ago=10)]
    )

    call_order: list[str] = []
    monkeypatch.setattr(
        reminder_check.db,
        "record_reminder_sent",
        lambda email_log_id, threshold: call_order.append("record") or True,
    )
    monkeypatch.setattr(
        reminder_check.slack_notify,
        "send_reminder_dm",
        lambda **kwargs: call_order.append("send"),
    )

    reminder_check.run_reminder_check()

    assert call_order == ["record", "send"]


def test_run_reminder_check_continues_independently_after_one_send_failure(monkeypatch) -> None:
    rows = [
        _row(id_="log1", contact_page_id="contact1", rep_email="rep-a@cnctor.jp", hours_ago=10),
        _row(id_="log2", contact_page_id="contact2", rep_email="rep-b@cnctor.jp", hours_ago=10),
    ]
    monkeypatch.setattr(reminder_check.db, "get_reminder_settings", lambda: (True, [3, 6]))
    monkeypatch.setattr(reminder_check.db, "find_latest_inbound_awaiting_reply", lambda: rows)

    recorded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        reminder_check.db,
        "record_reminder_sent",
        lambda email_log_id, threshold: recorded.append((email_log_id, threshold)) or True,
    )

    def flaky_send(**kwargs):
        if kwargs["rep_email"] == "rep-a@cnctor.jp":
            raise SlackDeliveryError("Slackユーザー解決に失敗しました")

    monkeypatch.setattr(reminder_check.slack_notify, "send_reminder_dm", flaky_send)

    result = reminder_check.run_reminder_check()

    # 送信記録は先に(送信の成否によらず)両方とも試みられる — record_reminder_sent()の
    # 設計上のトレードオフ通り、送信失敗した対象(log1)は次回再送されない。
    assert recorded == [("log1", 6), ("log2", 6)]
    assert result == {"eligible": 2, "sent": 1, "failed": 1}
