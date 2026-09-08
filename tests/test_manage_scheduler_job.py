"""実サービスへ接続せず、移行ジョブの停止復旧を検証する。"""
import json
import os
from pathlib import Path
import subprocess

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/cloud_run/manage_scheduler_job.sh"
URL = "https://example.run.app"
FAKE = '''#!/usr/bin/env python3
import json, os, pathlib, signal, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ["FAKE_ROOT"])
with (root / "calls").open("a") as f:
    f.write(json.dumps(args) + "\\n")
if args[:3] == ["run", "services", "describe"]:
    (root / "shell_pid").write_text(str(os.getppid()))
    print("https://example.run.app")
    sys.exit(0)
if args[:2] != ["scheduler", "jobs"]:
    sys.exit(0)
action = args[2]
p = root / "job.json"
job = json.loads(p.read_text())
if action == "list":
    if os.environ.get("FAIL") == "list":
        sys.exit(1)
    if os.environ.get("FAIL") == "existing":
        print("projects/test/locations/us-east4/jobs/crm-sfa-backend-token-encryption-healthcheck")
    sys.exit(0)
if os.environ.get("CREATE") == "1" and action == "describe" and not (root / "created").exists():
    sys.exit(1)
if action == "create":
    (root / "created").touch()
    job["state"] = "ENABLED"
    if os.environ.get("FAIL") == "already_exists":
        job["schedule"] = "0 1 * * *"
        p.write_text(json.dumps(job))
        sys.exit(1)
    p.write_text(json.dumps(job))
if action == "describe" and os.environ.get("FAIL") == "activation_mismatch" and job["state"] == "ENABLED":
    job["httpTarget"]["uri"] = "https://wrong.example/"
    print(json.dumps(job))
    sys.exit(0)
if action == "describe":
    if os.environ.get("FAIL") == "create_describe":
        sys.exit(1)
    if os.environ.get("FAIL") == "describe_after_pause" and (root / "paused").exists():
        sys.exit(1)
    print(job["state"] if "--format=value(state)" in args else json.dumps(job))
    sys.exit(0)
if action == "run":
    (root / "shell_pid").write_text(str(os.getppid()))
if action == "pause" and os.environ.get("FAIL") == "cleanup_signal":
    os.kill(int((root / "shell_pid").read_text()), signal.SIGTERM)
    sys.exit(1)
if action == "update":
    job["schedule"] = next(a.split("=", 1)[1] for a in args if a.startswith("--schedule="))
    p.write_text(json.dumps(job))
if action in ("resume", "pause"):
    if action == "pause":
        (root / "paused").touch()
    if os.environ.get("FAIL") != action and not (action == "pause" and os.environ.get("FAIL") == "pause_no_change"):
        job["state"] = "ENABLED" if action == "resume" else "PAUSED"
        p.write_text(json.dumps(job))
if action in ("run", "create") and os.environ.get("FAIL") in ("signal", "interrupt"):
    os.kill(os.getppid(), signal.SIGTERM if os.environ["FAIL"] == "signal" else signal.SIGINT)
if os.environ.get("FAIL") == action:
    sys.exit(1)
'''


@pytest.fixture
def rig(tmp_path):
    fake = tmp_path / "gcloud"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    job = {
        "attemptDeadline": "1800s", "state": "PAUSED", "schedule": "0 0 29 2 *", "timeZone": "Etc/UTC",
        "httpTarget": {"headers": {"X-Cloud-Scheduler": "true"}, "uri": URL + "/api/cron/token-encryption-healthcheck", "httpMethod": "GET",
                       "oidcToken": {"audience": URL, "serviceAccountEmail": "crm-sfa-backend-scheduler@test.iam.gserviceaccount.com"}},
    }
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "GCP_PROJECT_ID": "test", "GCP_REGION": "us-east4", "CLOUD_RUN_SERVICE": "crm-sfa-backend", "SKIP_SCHEDULE_GUARD": "1", "FAKE_ROOT": str(tmp_path)}

    def run(change=None, fail="", key="token-encryption-healthcheck", extra=(), action="run"):
        data = json.loads(json.dumps(job))
        if change:
            change(data)
        (tmp_path / "job.json").write_text(json.dumps(data))
        (tmp_path / "calls").write_text("")
        result = subprocess.run(["bash", str(SCRIPT), action, key, *extra], env={**env, "FAIL": fail, "CREATE": "1" if action == "create" else "0"}, text=True, capture_output=True, timeout=10)
        calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
        actions = [c[2] for c in calls if c[:2] == ["scheduler", "jobs"]]
        state = json.loads((tmp_path / "job.json").read_text())["state"]
        return result, actions, state
    return run


def test_run_resumes_then_restores_pause(rig):
    result, calls, state = rig()
    assert result.returncode == 0, result.stderr
    assert calls == ["describe", "resume", "run", "pause", "describe"]
    assert state == "PAUSED"
    assert "処理完了を意味しません" in result.stdout


@pytest.mark.parametrize("fail", ["resume", "run", "signal", "interrupt"])
def test_failure_and_interrupt_restore_pause(rig, fail):
    result, calls, state = rig(fail=fail)
    assert result.returncode != 0
    assert calls[-2:] == ["pause", "describe"]
    assert state == "PAUSED"


def test_pause_failure_reports_recovery(rig):
    result, calls, state = rig(fail="pause")
    assert result.returncode != 0
    assert state == "ENABLED"
    assert calls[-1] == "describe"
    assert "手動復旧:" in result.stderr


@pytest.mark.parametrize("field,value", [("state", "ENABLED"), ("schedule", "0 1 * * *"), ("timeZone", "UTC"), ("retryConfig", {"retryCount": 1}), ("retryConfig", {"maxRetryDuration": "60s"})])
def test_wrong_state_or_schedule_rejected(rig, field, value):
    result, calls, _ = rig(lambda job: job.update({field: value}))
    assert result.returncode != 0
    assert calls == ["describe"]


@pytest.mark.parametrize("field,value", [("uri", "https://wrong.example/"), ("httpMethod", "POST"), ("oidcToken", {"audience": "wrong", "serviceAccountEmail": "wrong"}), ("body", "eA=="), ("headers", {}), ("headers", {"X-Cloud-Scheduler": "false"})])
def test_wrong_target_rejected(rig, field, value):
    result, calls, _ = rig(lambda job: job["httpTarget"].update({field: value}))
    assert result.returncode != 0
    assert calls == ["describe"]


def test_business_write_requires_explicit_flag(rig):
    change = lambda job: job["httpTarget"].update(uri=URL + "/api/cron/daily-batch")
    result, calls, _ = rig(change, key="daily-batch")
    assert result.returncode != 0
    assert calls == ["describe"]
    result, calls, state = rig(change, key="daily-batch", extra=("--allow-business-writes",))
    assert result.returncode == 0
    assert state == "PAUSED"


def test_activate_rejects_existing_enabled_job(rig):
    result, calls, _ = rig(lambda job: job.update(state="ENABLED"), action="activate")
    assert result.returncode != 0
    assert calls == ["describe"]


@pytest.mark.parametrize("stamp,allowed", [
    ("2028-02-27T23:59:59+00:00", True),
    ("2028-02-28T00:00:00+00:00", False),
    ("2028-02-29T00:00:00+00:00", False),
    ("2028-03-01T00:00:00+00:00", False),
    ("2028-03-01T00:00:01+00:00", True),
    ("2100-03-01T00:00:00+00:00", True),
])
def test_temporary_schedule_date_guard(stamp, allowed):
    # 本体の判定コードを固定時刻で実行する。本番用の時刻上書き口は作らない。
    code = SCRIPT.read_text().split("<<'SAFE_DATE'\n", 1)[1].split("\nSAFE_DATE", 1)[0]
    code = code.replace("datetime.datetime.now(datetime.timezone.utc)",
                        f"datetime.datetime.fromisoformat({stamp!r})")
    result = subprocess.run(["python3", "-c", code], text=True, capture_output=True)
    assert (result.returncode == 0) is allowed



def test_activate_resume_failure_reports_updated_schedule(rig):
    result, calls, state = rig(fail="resume", action="activate")
    assert result.returncode != 0
    assert calls == ["describe", "update", "resume", "describe"]
    assert state == "PAUSED"
    assert "0 1 * * *" in result.stderr
    assert "日程は自動で戻しません" in result.stderr
    assert "旧cronの停止" in result.stderr
    assert "手動復旧:" in result.stderr


@pytest.mark.parametrize("fail", ["describe_after_pause", "pause_no_change"])
def test_cleanup_requires_confirmed_paused_state(rig, fail):
    result, calls, state = rig(fail=fail)
    assert result.returncode != 0
    assert calls[-2:] == ["pause", "describe"]
    assert "手動復旧:" in result.stderr
    if fail == "pause_no_change":
        assert state == "ENABLED"



def test_recovery_output_limits_http_fields():
    text = SCRIPT.read_text()
    assert "--format='yaml(state,schedule,timeZone,httpTarget.uri,httpTarget.httpMethod,httpTarget.oidcToken.audience,httpTarget.oidcToken.serviceAccountEmail)'" in text
    assert "yaml(state,schedule,timeZone,httpTarget)" not in text


@pytest.mark.parametrize("fail", ["", "create", "pause", "signal"])
def test_create_restores_pause_even_on_failure(rig, fail):
    result, calls, state = rig(fail=fail, action="create")
    assert (result.returncode == 0) is (not fail)
    assert calls[-2:] == ["pause", "describe"]
    assert state == ("ENABLED" if fail == "pause" else "PAUSED")
    if fail == "create":
        assert "通信結果不明" in result.stderr


def test_activate_requires_confirmation_and_verifies_result(rig):
    change = lambda job: job["httpTarget"].update(uri=URL + "/api/cron/daily-batch")
    result, calls, _ = rig(change, key="daily-batch", action="activate")
    assert result.returncode != 0
    assert calls == ["describe"]
    result, calls, state = rig(change, key="daily-batch", action="activate", extra=("--confirm-vercel-cron-removed",))
    assert result.returncode == 0, result.stderr
    assert calls == ["describe", "update", "resume", "describe"]
    assert state == "ENABLED"


@pytest.mark.parametrize("change", [
    lambda job: job.update(attemptDeadline="60s"),
    lambda job: job["httpTarget"]["headers"].update(Authorization="秘密値"),
])
def test_deadline_and_secret_headers_rejected(rig, change):
    result, calls, _ = rig(change)
    assert result.returncode != 0
    assert calls == ["describe"]
    assert "秘密値" not in result.stderr


@pytest.mark.parametrize("stamp,blocked", [
    ("2028-01-01T22:59:59+00:00", False),
    ("2028-01-01T23:00:00+00:00", False),
    ("2028-01-01T23:00:01+00:00", True),
    ("2028-01-02T00:59:59+00:00", True),
    ("2028-01-02T01:00:00+00:00", False),
])
def test_daily_guard_crosses_midnight(stamp, blocked):
    code = SCRIPT.read_text().split("<<'GUARD'\n", 1)[1].split("\nGUARD", 1)[0]
    code = code.replace("datetime.datetime.now(datetime.timezone.utc)", f"datetime.datetime.fromisoformat({stamp!r})")
    result = subprocess.run(["python3", "-c", code, "0 0 * * *"], text=True, capture_output=True)
    assert result.returncode == 0
    assert bool(result.stdout) is blocked



def test_activate_postcheck_mismatch_fails(rig):
    result, calls, _ = rig(fail="activation_mismatch", action="activate")
    assert result.returncode != 0
    assert "定時実行を有効化しました" not in result.stdout
    assert "手動復旧:" in result.stderr


def test_cleanup_can_be_interrupted(rig):
    result, _, _ = rig(fail="cleanup_signal")
    assert result.returncode != 0
    assert "停止確認が中断" in result.stderr
    assert "手動復旧:" in result.stderr


def test_run_failure_with_confirmed_pause_needs_no_pause_recovery(rig):
    result, _, state = rig(fail="run")
    assert result.returncode != 0
    assert state == "PAUSED"
    assert "停止済み" in result.stderr
    assert "手動復旧:" not in result.stderr


@pytest.mark.parametrize("fail", ["list", "existing"])
def test_create_existence_check_must_succeed_and_be_empty(rig, fail):
    result, calls, _ = rig(fail=fail, action="create")
    assert result.returncode != 0
    assert calls == ["list"]


@pytest.mark.parametrize("fail", ["already_exists", "create_describe"])
def test_create_cleanup_never_pauses_unverified_job(rig, fail):
    result, calls, state = rig(fail=fail, action="create")
    assert result.returncode != 0
    assert "pause" not in calls
    assert state == "ENABLED"
    assert "読取診断:" in result.stderr
    assert "手動復旧:" not in result.stderr


def test_create_cleanup_rejects_business_schedule(rig):
    result, calls, state = rig(lambda job: job.update(schedule="0 1 * * *"), action="create")
    assert result.returncode != 0
    assert "pause" not in calls
    assert state == "ENABLED"
    assert "読取診断:" in result.stderr
