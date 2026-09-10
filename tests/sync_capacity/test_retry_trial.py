"""限定試験の開始権・共有予約・成功診断。実サービス認証は使わない。"""
import json
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.capacity_trial import retry_policy as policy
from scripts.capacity_trial.guard import BASE, DATABASE, LEGACY_PROJECT, PROJECT, Ledger, Refused


@pytest.fixture
def retry_ledger(tmp_path, monkeypatch):
    path = tmp_path / "ledger.json"
    monkeypatch.setattr(policy, "LEDGER_PATH", path)
    now = 1788990000.0
    monkeypatch.setattr(policy, "wall_time", lambda: now)
    prefix = BASE + "/documents/sync_capacity_scopes/"
    names = [prefix + f"trial-concurrency-{n}" for n in range(1, 7)]
    names += [prefix + f"trial-concurrency-{n}/jobs/synthetic-{i}" for n in range(1, 7) for i in range(12)]
    names += [prefix + "trial-smoke/jobs/synthetic-" + str(i) for i in range(12)]
    data = {"project": PROJECT, "database": DATABASE, "created_at": 1788983820,
            "cost": None, "reserved": dict(policy.BASELINE), "rpc_calls": {"commit": 300}, "returned_documents": 3000,
            "target_migration": {"from_project": LEGACY_PROJECT, "from_database": "(default)", "at": 1788984000},
            "upper_bound_transition": {"created_at": 1788983820, "previous": {"basis": "reserved-upper-bound", "usd": 4},
                "reserved": {}, "rpc_calls": {}, "evidence": "合成履歴"},
            "upper_bound": {"basis": "reserved-upper-bound", "stage": 2, "usd": 8, "rpc_reserved": 1483,
                "document_names": names, "evidence": "合成履歴"}}
    path.write_text(json.dumps(data))
    monkeypatch.setattr(policy, "EXPECTED_HISTORY_SHA256", policy.fingerprint(policy.history(data)))
    return Ledger(path)


def test_start_once_and_auth_failure_consumes(retry_ledger, monkeypatch):
    from scripts.capacity_trial import retry_trial
    monkeypatch.delenv("CAPACITY_TRIAL_CHILD", raising=False)
    auth = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))
    monkeypatch.setattr(retry_trial.subprocess, "run", auth)
    args = SimpleNamespace(command="retry-deadline", role="runner", scope=policy.SCOPE)
    with pytest.raises(Refused):
        retry_trial.main(args, retry_ledger)
    assert retry_ledger.path.exists()
    assert json.loads(retry_ledger.path.read_text())["retry_trial"]["children_stopped"] is True
    with pytest.raises(Refused):
        retry_trial.main(args, retry_ledger)
    assert auth.call_count == 1


@pytest.mark.parametrize("change", [
    lambda d: d.update(halted=True),
    lambda d: d.update(created_at=1787000000),
    lambda d: d["upper_bound"].update(rpc_reserved=1484),
    lambda d: d["reserved"].update(reads=4439),
    lambda d: d["upper_bound_transition"].update(evidence="別の履歴"),
    lambda d: d["target_migration"].update(at=1788984001),
    lambda d: d["upper_bound"].update(stage=1),
    lambda d: d.update(cost={"usd": 2}),
    lambda d: d["upper_bound"]["document_names"].__setitem__(0, BASE + "/documents/sync_capacity_scopes/" + policy.SCOPE),
])
def test_changed_baseline_rejected_before_auth(retry_ledger, change, monkeypatch):
    from scripts.capacity_trial import retry_trial
    monkeypatch.delenv("CAPACITY_TRIAL_CHILD", raising=False)
    retry_ledger.transact(change)
    before = retry_ledger.path.read_bytes()
    auth = Mock()
    monkeypatch.setattr(retry_trial.subprocess, "run", auth)
    with pytest.raises(Refused):
        retry_trial.main(SimpleNamespace(command="retry-deadline", role="runner", scope=policy.SCOPE), retry_ledger)
    auth.assert_not_called()
    assert retry_ledger.path.read_bytes() == before


def test_unverified_history_and_other_ledger_refused(retry_ledger, monkeypatch):
    clone = retry_ledger.path.with_name("clone.json")
    clone.write_bytes(retry_ledger.path.read_bytes())
    with pytest.raises(Refused):
        policy.begin(Ledger(clone), "runner")
    monkeypatch.setattr(policy, "EXPECTED_HISTORY_SHA256", None)
    with pytest.raises(Refused):
        policy.begin(retry_ledger, "runner")


def test_observer_separate_budget_and_once(retry_ledger):
    policy.begin(retry_ledger, "runner")
    retry_ledger.reserve(reads=3000, writes=3200, runtime_seconds=660)
    for _ in range(400):
        retry_ledger.reserve(rpc=True)
    before = retry_ledger.path.read_bytes()
    with pytest.raises(Refused):
        retry_ledger.reserve(rpc=True)
    assert retry_ledger.path.read_bytes() == before
    with pytest.raises(Refused):
        policy.begin(retry_ledger, "observer")
    retry_ledger.transact(lambda d: d["retry_trial"].update(children_stopped=True))
    policy.begin(retry_ledger, "observer")
    retry_ledger.reserve(reads=102, runtime_seconds=60, rpc=True)
    retry_ledger.reserve(rpc=True)
    with pytest.raises(Refused):
        retry_ledger.reserve(writes=1)
    with pytest.raises(Refused):
        retry_ledger.reserve(rpc=True)
    with pytest.raises(Refused):
        policy.begin(retry_ledger, "observer")
    data = json.loads(retry_ledger.path.read_text())
    assert data["upper_bound"]["rpc_reserved"] == 1885
    assert data["reserved"]["writes"] == 3623


def _reserve_process(path, ticket, queue):
    # forkによる合成時計・固定パスの継承。親と同じ実ロックを競合させる。
    ledger = Ledger(path)
    ledger.retry_role, ledger.retry_ticket = "runner", ticket
    try:
        ledger.reserve(rpc=True, reads=20, writes=8)
        queue.put(True)
    except Refused:
        queue.put(False)


def test_parallel_boundary_has_no_excess_reservation(retry_ledger):
    ticket = policy.begin(retry_ledger, "runner")
    for _ in range(399):
        retry_ledger.reserve(rpc=True)
    context = multiprocessing.get_context("fork")
    queue = context.Queue()
    children = [context.Process(target=_reserve_process, args=(retry_ledger.path, ticket, queue)) for _ in range(12)]
    for child in children:
        child.start()
    for child in children:
        child.join(10)
        assert child.exitcode == 0
    assert sum(queue.get(timeout=2) for _ in children) == 1
    data = json.loads(retry_ledger.path.read_text())
    assert data["retry_trial"]["runner"]["used"] == {"rpc": 400, "reads": 20, "writes": 8, "runtime_seconds": 0}


def test_new_scope_cannot_bypass_through_other_commands(retry_ledger, monkeypatch):
    from scripts.capacity_trial import __main__ as runner
    for command in ("concurrency", "inspect-state", "scan", "claim-child", "smoke"):
        monkeypatch.setattr("sys.argv", ["trial", command, "--ledger", str(retry_ledger.path), "--scope", policy.SCOPE])
        with pytest.raises(Refused):
            runner.main()
    assert "retry_trial" not in json.loads(retry_ledger.path.read_text())


def test_start_ticket_cannot_be_reused_by_coordinator(retry_ledger):
    ticket = policy.begin(retry_ledger, "runner")
    policy.attach(retry_ledger, "runner", ticket)
    with pytest.raises(Refused):
        policy.attach(retry_ledger, "runner", ticket)
    with pytest.raises(Refused):
        policy.attach(retry_ledger, "runner", "wrong", child=True)


def test_more_than_five_names_and_changed_history_stop(retry_ledger):
    policy.begin(retry_ledger, "runner")
    names = [BASE + "/documents/sync_capacity_scopes/" + policy.SCOPE + "/jobs/" + str(i) for i in range(5)]
    retry_ledger.reserve(document_names=names)
    with pytest.raises(Refused):
        retry_ledger.reserve(document_names=[BASE + "/documents/sync_capacity_scopes/" + policy.SCOPE])
    retry_ledger.transact(lambda d: d["upper_bound_transition"].update(evidence="変更"))
    with pytest.raises(Refused):
        retry_ledger.reserve(rpc=True)


def test_first_3181ms_conflict_success_is_structured(monkeypatch):
    from google.api_core.exceptions import FailedPrecondition, ServiceUnavailable
    from src.sync_capacity.firestore_store import FirestoreJobStore, Settings
    from scripts.capacity_trial.retry_trial import validate_diagnostics
    clock = [0.0]
    monkeypatch.setattr("src.sync_capacity.deadline.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("src.sync_capacity.deadline.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr("src.sync_capacity.firestore_store.random.uniform", lambda *a: 0.03)
    diagnostics = []
    store = FirestoreJobStore(Settings("demo-local", "(default)", "trial", 3), client=Mock(), atomic_diagnostics=diagnostics)
    batches = []
    def operation(batch):
        batches.append(batch)
        if len(batches) == 1:
            clock[0] = 3.181
            error = FailedPrecondition("秘密を含むSDK本文")
            error.capacity_failure_origin = "guard_version_check"
            raise error
        return "success"
    assert store._atomic(operation) == "success"
    assert len(batches) == 2 and batches[0] is not batches[1]
    validate_diagnostics(diagnostics)
    assert diagnostics[0]["conflicts"][0]["elapsed_ms"] == 3181
    assert diagnostics[0]["retries"] == 1
    assert "秘密" not in json.dumps(diagnostics, ensure_ascii=False)
    operation = Mock(side_effect=ServiceUnavailable("結果不明"))
    with pytest.raises(ServiceUnavailable):
        store._atomic(operation)
    assert operation.call_count == 1
    assert diagnostics[-1]["outcome"] == "failed"


@pytest.mark.skipif(os.environ.get("FIRESTORE_EMULATOR_HOST") != "127.0.0.1:8787",
                    reason="公式ローカルemulator 127.0.0.1:8787 が必要")
def test_twelve_real_children_four_jobs_three_slots(retry_ledger, monkeypatch):
    import subprocess
    import sys
    import importlib.util
    from scripts.capacity_trial import concurrency
    helper = Path(__file__).with_name("retry_emulator_child.py")
    spec = importlib.util.spec_from_file_location("retry_emulator_child", helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import uuid
    project = "demo-retry-" + uuid.uuid4().hex[:16]
    module.configure(project, monkeypatch)
    retry_ledger.transact(lambda data: data.update(project=project))
    retry_ledger.transact(lambda data: data["upper_bound"].update(document_names=[
        name.replace("projects/actionpoint-autocalc/", "projects/" + project + "/")
        for name in data["upper_bound"]["document_names"]]))
    # 壁時計は親fixture・子helperとも合成値。元台帳の実期限には触れない。
    policy.begin(retry_ledger, "runner")
    policy.attach(retry_ledger, "runner", retry_ledger.retry_ticket)
    retry_ledger.reserve(runtime_seconds=60)
    original_popen = subprocess.Popen
    def local_child(command, **kwargs):
        kwargs["env"]["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:8787"
        kwargs["env"]["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        # 起動するのはローカル限定helper。元台帳やキーチェーンに触れるCLIではない。
        return original_popen([sys.executable, str(helper), str(retry_ledger.path), project], **kwargs)
    monkeypatch.setattr(concurrency.subprocess, "Popen", local_child)
    store = module.local_store(policy.SCOPE, "synthetic", retry_ledger)
    result = concurrency.retry_concurrency(store, "synthetic", retry_ledger)
    assert result["process_count"] == 12 and result["claimed_count"] == 3
    assert len(set(result["pids"])) == 12 and result["documents"] == 4
    assert result["children_stopped"] is True and len(result["diagnostics"]) == 12
    data = json.loads(retry_ledger.path.read_text())
    assert len(data["upper_bound"]["document_names"]) == 95
    assert data["retry_trial"]["runner"]["used"]["runtime_seconds"] == 660


@pytest.mark.parametrize("mode", ["early", "bad_ready", "bad_json", "duplicate_pid", "failed", "bad_diagnostics"])
def test_bad_real_child_never_passes_and_all_pids_stop(retry_ledger, monkeypatch, mode):
    import subprocess
    import sys
    from scripts.capacity_trial import concurrency
    policy.begin(retry_ledger, "runner")
    real_popen = subprocess.Popen
    processes = []
    script = '''import json,os,sys
sys.stdin.readline()
mode = sys.argv[1]
if mode == "early": sys.exit(1)
print(json.dumps({"ready": 0 if mode == "bad_ready" else os.getpid()}), flush=True)
sys.stdin.readline()
if mode == "failed": sys.exit(1)
if mode == "bad_json": print("invalid-json")
else: print(json.dumps({"pid": 0 if mode == "duplicate_pid" else os.getpid(), "claim": None}))
'''
    def spawn(command, **kwargs):
        process = real_popen([sys.executable, "-c", script, mode], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(concurrency.subprocess, "Popen", spawn)
    with pytest.raises((Refused, json.JSONDecodeError)):
        concurrency.run_claimants(policy.SCOPE, "synthetic", retry_ledger)
    assert len(processes) == 12
    assert all(process.poll() is not None for process in processes)


def test_observer_write_rejected_before_rpc(retry_ledger):
    from google.cloud.firestore_v1.types import Write, Document
    from scripts.capacity_trial.firestore import GuardedAPI
    policy.begin(retry_ledger, "runner")
    retry_ledger.transact(lambda d: d["retry_trial"].update(children_stopped=True))
    policy.begin(retry_ledger, "observer")
    raw = Mock()
    api = GuardedAPI(raw, retry_ledger, role="observer")
    before = retry_ledger.path.read_bytes()
    with pytest.raises(Refused):
        api.commit(request={"database": BASE, "writes": [Write(update=Document(
            name=BASE + "/documents/sync_capacity_scopes/" + policy.SCOPE))]})
    raw.commit.assert_not_called()
    assert retry_ledger.path.read_bytes() == before


@pytest.mark.parametrize("which", ["rpc", "reads", "runtime_seconds"])
def test_global_remaining_keeps_observer_reservation(retry_ledger, which):
    policy.begin(retry_ledger, "runner")
    def almost_full(data):
        if which == "rpc":
            data["upper_bound"]["rpc_reserved"] = 1998
        elif which == "reads":
            data["reserved"]["reads"] = 9898
        else:
            data["reserved"]["runtime_seconds"] = 86340
    retry_ledger.transact(almost_full)
    before = retry_ledger.path.read_bytes()
    with pytest.raises(Refused):
        retry_ledger.reserve(**({"rpc": True} if which == "rpc" else {which: 1}))
    assert retry_ledger.path.read_bytes() == before


def test_claim_whole_duration_over_thirty_is_trial_failure(monkeypatch):
    import io
    from scripts.capacity_trial import concurrency, firestore
    clock = [0.0]
    # 同期Mockだけの試験で製品と試験側の経過時計を揃える。SDKや実子は起動しない。
    monkeypatch.setattr(concurrency.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(concurrency, "validate_environment", lambda: None)
    monkeypatch.setattr("sys.stdin", io.StringIO("go\n"))
    store = Mock()
    store.claim.side_effect = lambda *args: clock.__setitem__(0, 31)
    monkeypatch.setattr(firestore, "make_store", lambda *args: store)
    with pytest.raises(Refused, match="30秒"):
        concurrency.claim_child(policy.SCOPE, Mock(), token="synthetic")


def test_preparation_keeps_product_thirty_under_coordinator_fortyfive(monkeypatch):
    from scripts.capacity_trial import concurrency
    from src.sync_capacity.deadline import CapacityDeadlineExceeded
    clock = [0.0]
    # 同期Mockだけの試験で製品と試験側の経過時計を揃える。SDKや実子は起動しない。
    monkeypatch.setattr(concurrency.time, "monotonic", lambda: clock[0])
    store = Mock()
    store.settings.scope = policy.SCOPE
    store.scope_ref.get.return_value.exists = False
    store.jobs.limit.return_value.stream.return_value = []
    def initialize(**kwargs):
        assert kwargs["deadline"].expires_at == 30
        clock[0] = 30.1
    store.initialize.side_effect = initialize
    with pytest.raises(CapacityDeadlineExceeded):
        concurrency.retry_concurrency(store, "synthetic", Mock())
    store.enqueue.assert_not_called()


@pytest.fixture
def stop_probe_timeout(monkeypatch):
    import io
    import subprocess
    from unittest.mock import MagicMock
    from scripts.capacity_trial import __main__ as runner
    process = MagicMock(pid=12345)
    process.__enter__.return_value = process
    process.stdin, process.stdout, process.stderr = io.StringIO(), io.StringIO(), io.StringIO()
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(runner.os, "waitid", Mock(return_value=SimpleNamespace(si_pid=12345)))
    monkeypatch.setattr(runner.os, "killpg", Mock(side_effect=PermissionError()))
    def run(command, **kwargs):
        if command[0] == "/bin/ps":
            raise subprocess.TimeoutExpired(command, 5)
        assert command[0] == "/usr/bin/security"
        return SimpleNamespace(returncode=0, stdout="synthetic-token")
    monkeypatch.setattr(runner.subprocess, "run", run)
    return runner


def test_stop_probe_timeout_is_not_execution_timeout(stop_probe_timeout, tmp_path):
    runner = stop_probe_timeout
    with pytest.raises(Refused, match="停止を確認できません"):
        runner.isolated_run(["synthetic"], cwd=tmp_path, env={}, input="", timeout=60)


def test_stop_probe_timeout_keeps_observer_blocked(retry_ledger, stop_probe_timeout, monkeypatch):
    from scripts.capacity_trial import retry_trial
    monkeypatch.delenv("CAPACITY_TRIAL_CHILD", raising=False)
    args = SimpleNamespace(command="retry-deadline", role="runner", scope=policy.SCOPE)
    with pytest.raises(Refused, match="停止を確認できません"):
        retry_trial.main(args, retry_ledger)
    data = json.loads(retry_ledger.path.read_text())
    assert data["retry_trial"]["children_stopped"] is False
    assert data["retry_trial"]["runner"]["passed"] is False
    with pytest.raises(Refused, match="停止未確認"):
        policy.begin(retry_ledger, "observer")


@pytest.mark.parametrize("payload", [
    {}, [], None,
    {"state": "passed"},
    {"state": "passed", "case": "retry-deadline", "project": PROJECT, "database": DATABASE,
     "scope": policy.SCOPE, "started_at": 1, "finished_at": 2, "result": {}},
    {"state": "observed", "case": "retry-deadline", "project": PROJECT, "database": DATABASE,
     "scope": policy.SCOPE, "started_at": 1, "finished_at": 2, "result": {"retained_state": {}}},
])
def test_invalid_success_json_keeps_failure_and_stop_record(retry_ledger, monkeypatch, payload):
    from scripts.capacity_trial import __main__ as runner, retry_trial
    monkeypatch.delenv("CAPACITY_TRIAL_CHILD", raising=False)
    monkeypatch.setattr(retry_trial.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=0, stdout="synthetic-token")))
    monkeypatch.setattr(runner, "isolated_run", Mock(return_value=SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr="")))
    args = SimpleNamespace(command="retry-deadline", role="runner", scope=policy.SCOPE)
    with pytest.raises(Refused):
        retry_trial.main(args, retry_ledger)
    record = json.loads(retry_ledger.path.read_text())["retry_trial"]
    assert record["children_stopped"] is True
    assert record["runner"]["passed"] is False
    assert record["runner"]["finished_at"] > 0
    assert record["runner"]["failure"]["error_type"] == "Refused"
    assert "retained_state" not in record["runner"]
    with pytest.raises(Refused):
        policy.begin(retry_ledger, "runner")


def test_valid_success_json_is_recorded_after_validation(retry_ledger, monkeypatch):
    from scripts.capacity_trial import __main__ as runner, retry_trial
    monkeypatch.delenv("CAPACITY_TRIAL_CHILD", raising=False)
    monkeypatch.setattr(retry_trial.subprocess, "run", Mock(return_value=SimpleNamespace(
        returncode=0, stdout="synthetic-token")))
    jobs = {str(index): {"state": "processing", "attempts": 1, "slot": str(index),
                       "owner": f"synthetic-process-{index + 1}"} for index in range(3)}
    jobs["3"] = {"state": "pending", "attempts": 0, "slot": None, "owner": None}
    retained = {"jobs": jobs, "slots": {str(index): {"job_id": str(index),
                "owner": f"synthetic-process-{index + 1}"} for index in range(3)}}
    payload = {"state": "passed", "case": "retry-deadline", "project": PROJECT, "database": DATABASE,
        "scope": policy.SCOPE, "started_at": 1, "finished_at": 2, "result": {
            "retained_state": retained, "process_count": 12, "claimed_count": 3, "documents": 4,
            "children_stopped": True, "slots_retained": True, "pids": list(range(1, 13))}}
    monkeypatch.setattr(runner, "isolated_run", Mock(return_value=SimpleNamespace(
        returncode=0, stdout=json.dumps(payload), stderr="")))
    args = SimpleNamespace(command="retry-deadline", role="runner", scope=policy.SCOPE)
    assert retry_trial.main(args, retry_ledger) == payload
    record = json.loads(retry_ledger.path.read_text())["retry_trial"]
    assert record["runner"]["passed"] is True
    assert record["runner"]["retained_state"] == retained
    assert record["children_stopped"] is True
