"""合成ログの突合と、部分取得を成功扱いしないことを確認する。"""

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from scripts.cloud_run.gmail_trial import app as module

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


def entry(run_id, event, age=600):
    at = (NOW - timedelta(seconds=age)).isoformat()
    return {"jsonPayload": {"job": "gmail-watch-renewal", "run_id": run_id,
                            "event": event, "started_at": at, "recorded_at": at}}


def test_complete_and_only_one_unfinished():
    done, unfinished = str(uuid4()), str(uuid4())
    rows = [entry(done, "started"), entry(unfinished, "started"), entry(done, "finished")]
    assert module.missing_runs(rows, NOW, 300) == [unfinished]
    assert module.missing_runs([rows[0], rows[2]], NOW, 300) == []


def test_grace_and_late_finish():
    run_id = str(uuid4())
    assert module.missing_runs([entry(run_id, "started", 299)], NOW, 300) == []
    rows = [entry(run_id, "started")]
    assert module.missing_runs(rows, NOW, 300) == [run_id]
    rows.append(entry(run_id, "finished"))
    assert module.missing_runs(rows, NOW, 300) == []


def test_finished_only_is_not_missing():
    assert module.missing_runs([entry(str(uuid4()), "finished")], NOW, 300) == []


@pytest.mark.parametrize("change", [{"run_id": "個人情報"}, {"started_at": "不正"},
                                     {"event": "unknown"}, {"job": "another"}])
def test_bad_payload_rejected(change):
    row = entry(str(uuid4()), "started")
    row["jsonPayload"].update(change)
    with pytest.raises((ValueError, KeyError, TypeError)):
        module.missing_runs([row], NOW, 300)


def test_all_pages_before_matching():
    run_id = str(uuid4())
    responses = iter([{"access_token": "fake"},
                      {"entries": [entry(run_id, "started")], "nextPageToken": "page2"},
                      {"entries": [entry(run_id, "finished")]}])
    rows = module.read_entries("trial-project", NOW - timedelta(days=1), NOW,
                               request_fn=lambda _, **kwargs: next(responses))
    assert module.missing_runs(rows, NOW, 300) == []


def test_page_failure_never_returns_partial_success():
    responses = iter([{"access_token": "fake"},
                      {"entries": [entry(str(uuid4()), "started")], "nextPageToken": "page2"}])
    def request(_, **kwargs):
        result = next(responses, None)
        if result is None:
            raise OSError("機密を含み得る外部例外")
        return result
    with pytest.raises(OSError):
        module.read_entries("trial-project", NOW - timedelta(days=1), NOW, request_fn=request)


def test_check_hides_error(monkeypatch, capsys):
    monkeypatch.setenv("GCP_PROJECT_ID", "trial-project")
    monkeypatch.setenv("TRIAL_SERVICE_NAME", module.SERVICE)
    def fail(*_):
        raise OSError("秘密の例外本文")
    monkeypatch.setattr(module, "read_entries", fail)
    response = TestClient(module.app).get("/check")
    assert response.status_code == 500
    output = capsys.readouterr().out
    assert '"event": "monitor_error"' in output
    assert "秘密" not in output + response.text
    assert "check_completed" not in output


def test_reject_production_service(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "trial-project")
    monkeypatch.setenv("TRIAL_SERVICE_NAME", "crm-sfa-backend")
    with pytest.raises(ValueError):
        module.settings()


@pytest.mark.parametrize("scenario,status,finished", [("success", 200, True),
    ("partial_failure", 200, True), ("http500", 500, True), ("unfinished", 200, False)])
def test_probe(scenario, status, finished, capsys):
    response = TestClient(module.app).post(f"/probe/{scenario}")
    assert response.status_code == status
    output = capsys.readouterr().out
    assert '"event":"started"' in output
    assert ('"event":"finished"' in output) == finished
    assert "@" not in output
    records = [json.loads(line) for line in output.splitlines()]
    assert {row["run_id"] for row in records} == {response.json()["run_id"]}
    assert [row["sequence"] for row in records] == list(range(1, len(records) + 1))
    counts = records[-1]["counts"]
    assert counts["renewed"] == 1
    assert counts["failed"] == int(scenario == "partial_failure")
    assert counts["attempted"] == (2 if scenario == "partial_failure" else 1)
    if finished:
        assert records[-1]["status"] == ("partial_failure" if scenario == "partial_failure" else "success")
        assert response.json()["counts"] == counts


@pytest.mark.parametrize("reverse", [False, True])
def test_mismatched_start_time_rejected_in_any_order(reverse):
    run_id = str(uuid4())
    rows = [entry(run_id, "started", 600), entry(run_id, "finished", 500)]
    with pytest.raises(ValueError, match="開始時刻が不一致"):
        module.missing_runs(rows[::-1] if reverse else rows, NOW, 300)


def test_check_zero_entries_is_not_arrival_confirmation(monkeypatch):
    monkeypatch.setenv("GCP_PROJECT_ID", "trial-project")
    monkeypatch.setenv("TRIAL_SERVICE_NAME", module.SERVICE)
    monkeypatch.setattr(module, "read_entries", lambda *_: [])
    payload = TestClient(module.app).get("/check").json()
    assert payload["entry_count"] == 0
    assert "未確認" in payload["message"]


@pytest.mark.parametrize("stage", ["configuration", "retrieval", "matching"])
def test_check_reports_safe_stage(monkeypatch, capsys, stage):
    monkeypatch.setenv("GCP_PROJECT_ID", "trial-project")
    monkeypatch.setenv("TRIAL_SERVICE_NAME", module.SERVICE)
    def fail(*_):
        raise ValueError("機密の本文")
    target = {"configuration": "settings", "retrieval": "read_entries", "matching": "missing_runs"}[stage]
    monkeypatch.setattr(module, "read_entries", lambda *_: [])
    monkeypatch.setattr(module, target, fail)
    response = TestClient(module.app).get("/check")
    assert response.status_code == 500
    assert response.json()["stage"] == stage
    assert "機密" not in response.text + capsys.readouterr().out


def test_search_uses_only_trial_log_view():
    requests = []
    def request(req, **kwargs):
        requests.append(req)
        return {"access_token": "fake"} if len(requests) == 1 else {}
    assert module.read_entries("trial-project", NOW - timedelta(days=1), NOW, request_fn=request) == []
    payload = json.loads(requests[1].data)
    assert payload["resourceNames"] == [
        "projects/trial-project/locations/global/buckets/crm-gmail-watch-trial/views/_AllLogs"
    ]
    assert 'resource.labels.service_name="crm-gmail-watch-trial"' in payload["filter"]


def test_total_search_budget_stops_before_next_page():
    times = iter([0, 0, 1, 1, 20, 41])
    responses = iter([{"access_token": "fake"}, {"nextPageToken": "next"}])
    calls = []
    def request(_, *, timeout):
        calls.append(timeout)
        return next(responses)
    with pytest.raises(TimeoutError, match="制限時間"):
        module.read_entries("trial-project", NOW, NOW, request_fn=request, clock=lambda: next(times))
    assert calls == [20, 20]


def test_last_page_timeout_is_limited_to_remaining_budget():
    times = iter([0, 0, 25, 25, 41])
    timeouts = []
    def request(_, *, timeout):
        timeouts.append(timeout)
        return {"access_token": "fake"} if len(timeouts) == 1 else {}
    with pytest.raises(TimeoutError, match="制限時間"):
        module.read_entries("trial-project", NOW, NOW, request_fn=request, clock=lambda: next(times))
    assert timeouts == [20, 15]


def test_repeated_page_token_is_error():
    responses = iter([{"access_token": "fake"}, {"nextPageToken": "same"}, {"nextPageToken": "same"}])
    with pytest.raises(ValueError, match="循環"):
        module.read_entries("trial-project", NOW, NOW, request_fn=lambda _, **kwargs: next(responses))



@pytest.mark.parametrize("future_seconds", [60, 61])
def test_clock_skew_boundary(future_seconds):
    row = entry(str(uuid4()), "started", -future_seconds)
    if future_seconds == 60:
        assert module.missing_runs([row], NOW, 300) == []
    else:
        with pytest.raises(ValueError, match="時刻の順序"):
            module.missing_runs([row], NOW, 300)


def test_recording_before_start_remains_invalid():
    row = entry(str(uuid4()), "started")
    row["jsonPayload"]["recorded_at"] = (NOW - timedelta(seconds=601)).isoformat()
    with pytest.raises(ValueError, match="時刻の順序"):
        module.missing_runs([row], NOW, 300)


@pytest.mark.parametrize("count", [20000, 20001])
def test_total_entry_limit(count):
    responses = iter([{"access_token": "fake"}, {"entries": [{}] * 10000, "nextPageToken": "next"},
                      {"entries": [{}] * (count - 10000)}])
    def read():
        return module.read_entries("trial-project", NOW, NOW, request_fn=lambda _, **kwargs: next(responses))
    if count == 20000:
        assert len(read()) == 20000
    else:
        with pytest.raises(ValueError, match="件数上限"):
            read()


@pytest.mark.parametrize("error,kind,status", [
    (module.HTTPError("秘密URL", 403, "秘密本文", {}, None), "http", 403),
    (module.HTTPError("秘密URL", 999, "秘密本文", {}, None), "http", None),
    (module.HTTPError("秘密URL", "403", "秘密本文", {}, None), "http", None),
    (TimeoutError("秘密本文"), "timeout", None),
    (module.URLError(TimeoutError("秘密本文")), "timeout", None),
    (ValueError("秘密本文"), "invalid", None),
    (RuntimeError("秘密本文"), "other", None),
])
def test_safe_error_classification(monkeypatch, capsys, error, kind, status):
    monkeypatch.setenv("GCP_PROJECT_ID", "trial-project")
    monkeypatch.setenv("TRIAL_SERVICE_NAME", module.SERVICE)
    def fail(*_):
        raise error
    monkeypatch.setattr(module, "read_entries", fail)
    response = TestClient(module.app).get("/check")
    output = capsys.readouterr().out
    record = json.loads(output)
    assert record["error_kind"] == kind
    assert record.get("http_status") == status
    assert ("http_status" in record) == (status is not None)
    assert response.status_code == 500
    assert "秘密" not in output + response.text
