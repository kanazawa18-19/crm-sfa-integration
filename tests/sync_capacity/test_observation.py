"""外向き通信なしの合成時計・保存障害・500件超の観測試験。"""

import logging
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.sync_capacity.application import drain_one
from src.sync_capacity.domain import Claim
from src.sync_capacity.observation import observe_queue


@pytest.fixture(autouse=True)
def capture_telemetry(caplog):
    from src.sync_capacity.telemetry import logger
    logger.addHandler(caplog.handler)
    yield
    logger.removeHandler(caplog.handler)


def events(caplog):
    return [record.sync_capacity for record in caplog.records if hasattr(record, "sync_capacity")]


def test_phase_times_ignore_wall_clock_and_do_not_log_body(caplog):
    caplog.set_level(logging.INFO)
    store = Mock(claim=Mock(return_value=Claim("job", "owner", "0", "notion", {"secret": "PRIVATE"})))
    ticks = iter([0, 1, 3, 4, 7, 8, 15, 16, 20])
    drain_one(store, lambda _: lambda: ({"statusCode": 200, "body": "{}"}, False),
              monotonic=lambda: next(ticks), clock=Mock(side_effect=[100, -100]))
    final = events(caplog)[-1]
    assert final["state"] == "completed"
    assert [final[k] for k in ("claim_seconds", "initialization_seconds", "execution_seconds",
                               "finish_seconds", "attempt_seconds")] == [2, 3, 7, 4, 20]
    assert "PRIVATE" not in caplog.text
    assert '"execution_seconds": 7' in caplog.text


def test_initialization_and_finish_failure_leave_execution_missing(caplog):
    caplog.set_level(logging.INFO)
    store = Mock(claim=Mock(return_value=Claim("job", "owner", "0", "notion", {})),
                 finish=Mock(side_effect=TimeoutError("PRIVATE")))
    with pytest.raises(TimeoutError):
        drain_one(store, Mock(side_effect=ValueError("PRIVATE")))
    final = events(caplog)[-1]
    assert final["event"] == "finish_unconfirmed"
    assert final["intended_state"] == "retry"
    assert final["execution_seconds"] is None
    assert "PRIVATE" not in caplog.text
    store.finish.assert_called_once()


def test_idle_and_claim_unknown_are_observed(caplog):
    caplog.set_level(logging.INFO)
    prepare = Mock()
    drain_one(Mock(claim=Mock(return_value=None)), prepare)
    assert events(caplog)[-1]["event"] == "idle_or_full"
    with pytest.raises(TimeoutError):
        drain_one(Mock(claim=Mock(side_effect=TimeoutError())), prepare)
    assert events(caplog)[-1]["event"] == "claim_unconfirmed"
    prepare.assert_not_called()


def test_process_termination_has_no_fake_end_or_release(caplog):
    caplog.set_level(logging.INFO)
    store = Mock(claim=Mock(return_value=Claim("job", "owner", "0", "notion", {})))
    with pytest.raises(SystemExit):
        drain_one(store, lambda _: Mock(side_effect=SystemExit()))
    assert events(caplog)[-1]["event"] == "initialization_finished"
    store.finish.assert_not_called()


def queue(rows):
    jobs = Mock()
    jobs.select.return_value.stream.return_value = (
        SimpleNamespace(to_dict=lambda row=row: row) for row in rows)
    return jobs


def test_full_scan_counts_beyond_inspect_limit_and_finds_oldest():
    jobs = queue([{"state": "pending", "created_at": 90}] * 600 +
                 [{"state": "retry", "created_at": 1}, {"state": "completed"}])
    result = observe_queue(jobs, clock=lambda: 100)
    assert result["total"] == 602
    assert result["counts"]["pending"] == 600
    assert result["oldest_waiting_age_seconds"] == 99
    assert result["consistency"] == "full_scan_not_atomic_snapshot"
    jobs.select.assert_called_once_with(["state", "created_at"])


@pytest.mark.parametrize("bad", [None, True, float("nan"), float("inf"), 101, "1"])
def test_missing_or_invalid_age_is_not_zero(bad):
    result = observe_queue(queue([{"state": "pending", "created_at": bad},
                                  {"state": "pending", "created_at": 1}]), clock=lambda: 100)
    assert result["counts"]["pending"] == 2
    assert result["invalid_waiting_timestamp_count"] == 1
    assert result["oldest_waiting_age_seconds"] is None


def test_scan_failure_never_returns_partial_success():
    def interrupted():
        yield SimpleNamespace(to_dict=lambda: {"state": "pending", "created_at": 1})
        raise TimeoutError("PRIVATE")
    jobs = Mock()
    jobs.select.return_value.stream.return_value = interrupted()
    with pytest.raises(TimeoutError):
        observe_queue(jobs)


def test_default_process_writes_metrics_to_stderr():
    import os
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-c", '''
from unittest.mock import Mock
from src.sync_capacity.application import drain_one
drain_one(Mock(claim=Mock(return_value=None)), Mock())
'''], env={"PATH": os.defpath, "PYTHONPATH": "."}, capture_output=True, text=True, check=True)
    assert '"event": "idle_or_full"' in result.stderr
    assert '"claim_seconds":' in result.stderr
    assert result.stdout == ""


def test_logging_failure_does_not_turn_saved_result_into_retry(monkeypatch):
    from src.sync_capacity.telemetry import logger
    monkeypatch.setattr(logger, "log", Mock(side_effect=OSError("broken log sink")))
    store = Mock(claim=Mock(return_value=Claim("job", "owner", "0", "notion", {})))
    assert drain_one(store, lambda _: lambda: ({"statusCode": 200}, False))["state"] == "completed"
    store.finish.assert_called_once()


def test_worker_busy_and_store_failure_are_observed(monkeypatch, caplog):
    from src.sync_capacity import worker
    factory = Mock(side_effect=RuntimeError("PRIVATE"))
    monkeypatch.setattr(worker, "get_store", factory)
    worker._WORKER_LOCK.acquire()
    try:
        assert worker.run_worker()["state"] == "local_worker_busy"
    finally:
        worker._WORKER_LOCK.release()
    factory.assert_not_called()
    assert events(caplog)[-1]["event"] == "local_worker_busy"
    with pytest.raises(RuntimeError):
        worker.run_worker()
    assert not worker._WORKER_LOCK.locked()
    event = events(caplog)[-1]
    assert event["event"] == "worker_store_unavailable"
    assert event["severity"] == "WARNING"
    assert event["timestamp"].endswith("+00:00")
    assert "PRIVATE" not in caplog.text
