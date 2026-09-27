"""日報が失敗しても掃除が実行済みであり、掃除失敗も日報を妨げない。"""

import pytest

from src.api.routes import cron


def test_cleanup_finishes_before_report_failure(monkeypatch):
    calls = []

    def cleanup():
        calls.append("cleanup")
        return 12

    def report():
        assert calls == ["cleanup"]
        raise TimeoutError("日報の時間切れ")

    monkeypatch.setattr(cron, "purge_old_events", cleanup)
    monkeypatch.setattr(cron, "run_report_batch", report)
    with pytest.raises(TimeoutError):
        cron.run_daily_batch()
    assert calls == ["cleanup"]


def test_cleanup_failure_preserves_report_and_hides_exception(monkeypatch, caplog):
    def cleanup():
        raise RuntimeError("秘密の接続文字列")

    monkeypatch.setattr(cron, "purge_old_events", cleanup)
    monkeypatch.setattr(cron, "run_report_batch", lambda: {"daily_report_sent": True})
    assert cron.run_daily_batch() == {"daily_report_sent": True}
    assert "掃除に失敗" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "秘密の接続文字列" not in caplog.text


def test_cleanup_count_is_returned(monkeypatch):
    monkeypatch.setattr(cron, "purge_old_events", lambda: 12)
    monkeypatch.setattr(cron, "run_report_batch", lambda: {"daily_report_sent": True})
    assert cron.run_daily_batch() == {"daily_report_sent": True, "purged_webhook_events": 12}


def test_cleanup_bounds_database_wait_before_delete(monkeypatch):
    from unittest.mock import MagicMock
    from src.sync_engine import webhook_events

    connection = MagicMock()
    cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
    cursor.rowcount = 12
    monkeypatch.setattr(webhook_events, "_connect", lambda: connection)
    assert webhook_events.purge_old_events() == 12
    statements = [call.args[0] for call in cursor.execute.call_args_list]
    assert statements[:2] == [
        "SET LOCAL lock_timeout = '2s'",
        "SET LOCAL statement_timeout = '10s'",
    ]
    assert statements[2].startswith('DELETE FROM "WebhookEvent"')
    assert cursor.execute.call_args_list[2].args[1] == (7,)
