"""実サービスへ接続せず、接続前拒否と予算停止を検証する。"""
import json
from unittest.mock import Mock

import pytest

from scripts.capacity_trial.guard import (
    BASE, DATABASE, LEGACY_PROJECT, PROJECT, Ledger, Refused, validate_environment, validate_neon,
    validate_resource, validate_target,
)
from scripts.capacity_trial.firestore import GuardedAPI
from src.sync_capacity.domain import submission


@pytest.fixture
def ledger(tmp_path):
    item = Ledger.initialize(tmp_path / "ledger.json", 1000, now=1000)
    item.cost(0.1, 1000, "合成ローカルテスト値", now=1000)
    return item


@pytest.mark.parametrize("project,database,scope,slots,role", [
    ("fabled-electron-406310", "(default)", "trial-smoke", 3, "runner"),
    (PROJECT, "(default)", "trial-smoke", 3, "runner"),
    (LEGACY_PROJECT, DATABASE, "trial-smoke", 3, "runner"),
    (PROJECT, "capacity-restore", "trial-smoke", 3, "runner"),
    (PROJECT, DATABASE, "production", 3, "runner"),
    (PROJECT, DATABASE, "trial-arbitrary", 3, "runner"),
    (PROJECT, DATABASE, "trial-smoke", 4, "runner"),
    (PROJECT, DATABASE, "trial-smoke", 3, "owner"),
])
def test_target_refuses_before_client(project, database, scope, slots, role):
    with pytest.raises(Refused):
        validate_target(project, database, scope, slots, role)


def test_environment_rejects_inheritance_and_nonempty_home(tmp_path):
    env = {"HOME": str(tmp_path), "CAPACITY_TRIAL_CHILD": "1", "PYTHONNOUSERSITE": "1"}
    validate_environment(env)
    for name in ["DATABASE_URL", "GOOGLE_APPLICATION_CREDENTIALS", "FIRESTORE_EMULATOR_HOST",
                 "PYTHONPATH", "HTTPS_PROXY", "CLOUDSDK_CONFIG", "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"]:
        with pytest.raises(Refused):
            validate_environment({**env, name: "secret-not-logged"})
    (tmp_path / ".config").mkdir()
    with pytest.raises(Refused):
        validate_environment(env)


def test_neon_is_closed_until_exact_identity_exists():
    with pytest.raises(Refused):
        validate_neon("postgresql://test:secret@production.neon.tech/neondb")


@pytest.mark.parametrize("resource", [
    "projects/fabled-electron-406310/databases/(default)",
    BASE + "/documents/sync_capacity_scopes/production",
    BASE + "/documents/sync_capacity_scopes/trial-smoke/jobs/a/secret/b",
    BASE + "/documents/other/a",
])
def test_resource_escape_rejected(resource):
    with pytest.raises(Refused):
        validate_resource(resource)


def test_budget_stale_expiry_and_ceiling(ledger):
    ledger.reserve(now=1000, reads=2_999_999)
    with pytest.raises(Refused):
        ledger.reserve(now=1000, reads=2)
    assert json.loads(ledger.path.read_text())["reserved"]["reads"] == 2_999_999
    with pytest.raises(Refused):
        ledger.reserve(now=4601)
    with pytest.raises(Refused):
        ledger.reserve(now=1000+14*86400)
    ledger.cost(10, 1000, "合成", now=1000)
    with pytest.raises(Refused):
        ledger.reserve(now=1000)


def test_missing_cost_cannot_be_treated_as_zero(tmp_path):
    item = Ledger.initialize(tmp_path / "ledger.json", 1000, now=1000)
    with pytest.raises(Refused):
        item.reserve(now=1000)


def test_cost_cannot_rewind(ledger):
    with pytest.raises(Refused):
        ledger.cost(0, 1000, "合成", now=1000)
    with pytest.raises(Refused):
        ledger.cost(0.2, 999, "合成", now=1000)


def test_rpc_rejects_remote_before_network():
    raw = Mock()
    api = GuardedAPI(raw, Mock(), role="runner")
    with pytest.raises(Refused):
        api.batch_get_documents(request={"database": BASE, "documents": [
            "projects/fabled-electron-406310/databases/(default)/documents/a/b"]})
    raw.batch_get_documents.assert_not_called()


def test_rpc_forces_no_retry_and_records_stream():
    from google.cloud.firestore_v1.types import BatchGetDocumentsResponse, Document
    raw, budget = Mock(), Mock()
    name = BASE + "/documents/sync_capacity_scopes/trial-smoke/jobs/a"
    raw.batch_get_documents.return_value = iter([BatchGetDocumentsResponse(found=Document(name=name))])
    api = GuardedAPI(raw, budget, role="runner")
    assert len(list(api.batch_get_documents(request={"database": BASE, "documents": [name]},
                                           retry=object(), timeout=999))) == 1
    assert raw.batch_get_documents.call_args.kwargs["retry"] is None
    assert raw.batch_get_documents.call_args.kwargs["timeout"] == 55
    budget.reserve.assert_called_once_with(reads=1, writes=0)


def test_observer_write_and_unknown_rpc_are_local_refusals():
    api = GuardedAPI(Mock(), Mock(), role="observer")
    with pytest.raises(Refused):
        api.commit(request={"database": BASE, "writes": []})
    with pytest.raises(Refused):
        api.delete_document


def test_interrupted_stream_does_not_return_partial_success():
    from google.cloud.firestore_v1.types import RunQueryResponse, Document
    from src.sync_capacity.observation import observe_queue
    raw, budget = Mock(), Mock()
    def interrupted():
        yield RunQueryResponse(document=Document(name=BASE + "/documents/sync_capacity_scopes/trial-smoke/jobs/a"))
        raise TimeoutError("合成応答途絶")
    raw.run_query.return_value = interrupted()
    api = GuardedAPI(raw, budget, role="observer")
    query = {"from_": [{"collection_id": "jobs"}]}
    stream = api.run_query(request={"parent": BASE + "/documents/sync_capacity_scopes/trial-smoke",
                                    "structured_query": query})
    assert next(stream).document.name
    with pytest.raises(TimeoutError):
        next(stream)
    raw.run_query.assert_called_once()


def test_actual_clean_child_environment(tmp_path):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-s", "-c",
        "from scripts.capacity_trial.guard import validate_environment; validate_environment()"],
        env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
             "PYTHONNOUSERSITE": "1", "CAPACITY_TRIAL_CHILD": "1"},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_real_sdk_reads_pass_through_guard_without_network():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore
    from google.cloud.firestore_v1.types import BatchGetDocumentsResponse, Document, RunQueryResponse
    from google.cloud.firestore_v1 import _helpers
    from src.sync_capacity.firestore_store import FirestoreJobStore, Settings
    scope = BASE + "/documents/sync_capacity_scopes/trial-smoke"
    raw = Mock()
    raw.batch_get_documents.return_value = iter([BatchGetDocumentsResponse(found=Document(
        name=scope, fields=_helpers.encode_dict({"version": 1, "limit": 3,
                                                "slots": {"0": None, "1": None, "2": None}})))])
    raw.run_query.return_value = iter([RunQueryResponse(document=Document(
        name=scope+"/jobs/a", fields=_helpers.encode_dict({"state": "pending", "created_at": 1})))])
    client = firestore.Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())
    client._firestore_api_internal = GuardedAPI(raw, Mock(), role="observer")
    store = FirestoreJobStore(Settings(PROJECT, DATABASE, "trial-smoke", 3), client=client)
    assert store.observe()["total"] == 1
    raw.commit.assert_not_called()


@pytest.mark.parametrize("suffix", ["?hostaddr=127.0.0.1", "?service=production", "?sslrootcert=/tmp/x",
                                      "?options=-csearch_path=public", "?host=production.neon.tech"])
def test_neon_connection_override_is_rejected(suffix):
    from scripts.capacity_trial.guard import NEON_HOST
    with pytest.raises(Refused):
        validate_neon(f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb"+suffix)


def test_neon_exact_target_and_certificate_verification():
    from scripts.capacity_trial.guard import NEON_HOST
    dsn = f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb?sslmode=require&channel_binding=require"
    values = validate_neon(dsn)
    import certifi
    assert values["sslmode"] == "verify-full" and values["sslrootcert"] == certifi.where()
    for bad in [dsn.replace(NEON_HOST, NEON_HOST.replace(".c-11", "-pooler.c-11")),
                dsn.replace("neondb_owner", "other"), dsn.replace("/neondb?", "/other?"),
                dsn.replace("sslmode=require", "sslmode=disable")]:
        with pytest.raises(Refused):
            validate_neon(bad)


@pytest.mark.parametrize("closed", [True, False])
def test_neon_probe_checks_two_sessions_and_closes(monkeypatch, closed):
    from unittest.mock import MagicMock
    from scripts.capacity_trial import neon
    from scripts.capacity_trial.guard import NEON_HOST
    monkeypatch.setattr(neon, "validate_environment", lambda: None)
    first, second = MagicMock(), MagicMock()
    for connection in [first, second]:
        connection.closed = True
        connection.__enter__.return_value = connection
    first.execute.return_value.fetchone.side_effect = [("neondb", "neondb_owner", 1), (True,), (True,)]
    second.execute.return_value.fetchone.side_effect = [("neondb", "neondb_owner", 2), (False,), (True,), (True,)]
    connector = Mock(side_effect=[first, second])
    monkeypatch.setattr("psycopg.connect", connector)
    second.closed = closed
    if not closed:
        with pytest.raises(AssertionError, match="終了後もSQL接続"):
            neon.probe(f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb?sslmode=require", Mock())
        first.__exit__.assert_called_once()
        second.__exit__.assert_called_once()
        return
    result = neon.probe(f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb?sslmode=require", Mock())
    assert result["connection_count"] == connector.call_count == 2
    assert result["advisory_exclusion"] and result["connections_closed"]
    assert result["sql_count"] == first.execute.call_count+second.execute.call_count == 7
    first.__exit__.assert_called_once()
    second.__exit__.assert_called_once()


def test_neon_failure_still_closes_all_connections(monkeypatch):
    from unittest.mock import MagicMock
    from scripts.capacity_trial import neon
    from scripts.capacity_trial.guard import NEON_HOST
    monkeypatch.setattr(neon, "validate_environment", lambda: None)
    first, second = MagicMock(), MagicMock()
    for connection in [first, second]:
        connection.closed = True
        connection.__enter__.return_value = connection
        connection.execute.side_effect = TimeoutError("合成")
    monkeypatch.setattr("psycopg.connect", Mock(side_effect=[first, second]))
    with pytest.raises(TimeoutError):
        neon.probe(f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb?sslmode=require", Mock())
    first.__exit__.assert_called_once()
    second.__exit__.assert_called_once()


def test_scan_offset_is_rejected_before_network():
    raw = Mock()
    api = GuardedAPI(raw, Mock(), role="observer")
    with pytest.raises(Refused):
        api.run_query(request={"parent": BASE+"/documents/sync_capacity_scopes/trial-smoke",
                               "structured_query": {"from_": [{"collection_id": "jobs"}], "offset": 99999, "limit": 1}})
    raw.run_query.assert_not_called()


def test_scan_reserves_sentinel_and_rejects_overflow():
    from google.cloud.firestore_v1.types import RunQueryResponse, Document
    raw, budget = Mock(), Mock()
    response = RunQueryResponse(document=Document(name=BASE+"/documents/sync_capacity_scopes/trial-smoke/jobs/a"))
    raw.run_query.return_value = (response for _ in range(100001))
    api = GuardedAPI(raw, budget, role="observer")
    with pytest.raises(Refused):
        list(api.run_query(request={"parent": BASE+"/documents/sync_capacity_scopes/trial-smoke",
                                   "structured_query": {"from_": [{"collection_id": "jobs"}]}}))
    budget.reserve.assert_called_once_with(reads=100001, writes=0)
    assert raw.run_query.call_args.kwargs["request"]["structured_query"].limit == 100001


def test_smoke_will_not_reuse_an_old_completed_job():
    from scripts.capacity_trial.__main__ import smoke
    store = Mock()
    store.jobs.limit.return_value.stream.return_value = iter([Mock()])
    with pytest.raises(Refused):
        smoke(store)
    store.enqueue.assert_not_called()
    store.initialize.assert_not_called()


def test_free_plan_basis_is_distinct_from_metered_cost(ledger):
    # 過去の有料累計を無料根拠で消すことはできない。
    with pytest.raises(Refused):
        ledger.cost(0, 1000, "Free契約確認", now=1000, basis="free-plan-verified")


def test_public_error_codes_are_fixed_and_do_not_include_secrets():
    assert Refused("専用キーチェーン認証情報が取得できません").code == "credential_unavailable"
    assert Refused("期限・費用停止値・費用取得途絶のため新規実行停止").code == "budget_stopped"
    assert Refused("60秒で子を停止。結果不明の枠は自動回収しません").code == "trial_timeout"
    assert Refused("synthetic-secret", code="synthetic-secret").code == "guard_refused"


@pytest.mark.parametrize("child_code,expected", [("budget_stopped", "budget_stopped"),
                                                  ("synthetic-secret", "child_failed")])
def test_launcher_only_propagates_known_child_error_codes(tmp_path, monkeypatch, child_code, expected):
    import subprocess
    import sys
    from scripts.capacity_trial import __main__ as runner
    monkeypatch.delenv("CAPACITY_TRIAL_CHILD", raising=False)
    monkeypatch.setattr(sys, "argv", ["trial", "neon-probe", "--ledger", str(tmp_path / "ledger.json")])
    monkeypatch.setattr(runner.Ledger, "reserve", lambda *a, **kw: None)
    monkeypatch.setattr(runner.Ledger, "authorize_command", lambda *a, **kw: None)
    responses = iter([
        subprocess.CompletedProcess([], 0, stdout="synthetic-secret", stderr=""),
        subprocess.CompletedProcess([], 1, stdout="", stderr="SDK synthetic-secret\nCAPACITY_TRIAL_FAILURE_V1 " + json.dumps({
            "state": "failed", "error_code": child_code, "error_type": "Refused", "partial_result": False})),
    ])
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: next(responses))
    monkeypatch.setattr(runner, "isolated_run", lambda *a, **kw: next(responses))
    with pytest.raises(Refused) as captured:
        runner.main()
    assert captured.value.code == expected
    assert "synthetic-secret" not in str(captured.value)


def test_neon_missing_trusted_ca_bundle_is_rejected(tmp_path, monkeypatch):
    from scripts.capacity_trial.guard import NEON_HOST
    monkeypatch.setattr("certifi.where", lambda: str(tmp_path / "missing-ca.pem"))
    with pytest.raises(Refused):
        validate_neon(f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb?sslmode=require")


@pytest.fixture
def legacy_ledger(ledger):
    def legacy(data):
        data["project"] = LEGACY_PROJECT
        data.pop("database")
        data["reserved"].update(runtime_seconds=145, sql_connections=5, sql_statements=24)
    ledger.transact(legacy)
    return ledger


def test_migration_preserves_deadline_totals_and_requires_new_metered_cost(legacy_ledger):
    item = legacy_ledger
    before = json.loads(item.path.read_text())
    item.migrate_target(now=1100)
    after = json.loads(item.path.read_text())
    for key in ("created_at", "reserved", "rpc_calls", "returned_documents", "cost"):
        assert after[key] == before[key]
    assert after["project"] == PROJECT and after["database"] == DATABASE
    with pytest.raises(Refused):
        item.reserve(now=1100)
    for observed_at, basis in [(1099, "metered"), (1100, "free-plan-verified")]:
        with pytest.raises(Refused):
            item.cost(0.1, observed_at, "合成再照合", now=1100, basis=basis)
    item.cost(0.2, 1100, "合成再照合", now=1100)
    item.reserve(now=1100, reads=1)
    assert json.loads(item.path.read_text())["reserved"]["sql_statements"] == 24
    with pytest.raises(Refused):
        item.reserve(now=1000+14*86400)
    with pytest.raises(Refused):
        item.migrate_target(now=1101)


@pytest.mark.parametrize("change", [
    {"project": PROJECT}, {"database": "other"}, {"returned_documents": 1},
    {"rpc_calls": {"run_query": 1}},
    *[{"reserved": {"reads": 0, "writes": 0, "deletes": 0, kind: 1}}
      for kind in ("reads", "writes", "deletes")],
])
def test_migration_rejects_used_or_unexpected_source_without_mutation(legacy_ledger, change):
    legacy_ledger.transact(lambda data: data.update(change))
    before = legacy_ledger.path.read_bytes()
    with pytest.raises(Refused):
        legacy_ledger.migrate_target(now=1100)
    assert legacy_ledger.path.read_bytes() == before


def test_named_database_is_allowed_and_default_resource_is_rejected():
    validate_target(PROJECT, DATABASE, "trial-smoke")
    with pytest.raises(Refused):
        validate_resource(f"projects/{PROJECT}/databases/(default)")


def test_migration_cannot_resume_using_previous_free_plan(legacy_ledger):
    legacy_ledger.transact(lambda data: data["cost"].update(usd=0, basis="free-plan-verified"))
    legacy_ledger.migrate_target(now=1100)
    with pytest.raises(Refused):
        legacy_ledger.cost(0, 1100, "旧Free契約", now=1100, basis="free-plan-verified")
    with pytest.raises(Refused):
        legacy_ledger.reserve(now=1100)


def test_migration_cli_does_not_fetch_credentials(legacy_ledger, monkeypatch):
    import sys
    from scripts.capacity_trial import __main__ as runner
    monkeypatch.setattr(sys, "argv", ["trial", "migrate-target", "--ledger", str(legacy_ledger.path)])
    network = Mock(side_effect=AssertionError("認証・子起動は禁止"))
    monkeypatch.setattr(runner.subprocess, "run", network)
    result = runner.main()
    assert result["state"] == "target_migrated"
    assert result["project"] == PROJECT and result["database"] == DATABASE
    network.assert_not_called()


def test_new_target_rejects_free_cost_even_after_metered_zero(ledger):
    ledger.transact(lambda data: data.update(cost=None))
    for already_metered in (False, True):
        if already_metered:
            ledger.cost(0, 1000, "実費の合成値", now=1000)
        with pytest.raises(Refused):
            ledger.cost(0, 1000, "旧Free", now=1000, basis="free-plan-verified")
    ledger.transact(lambda data: data["cost"].update(basis="free-plan-verified"))
    with pytest.raises(Refused):
        ledger.reserve(now=1000)


def test_scan_runner_rejected_before_credentials(tmp_path, monkeypatch):
    import sys
    from scripts.capacity_trial import __main__ as runner
    monkeypatch.setattr(sys, "argv", ["trial", "scan", "--ledger", str(tmp_path / "ledger.json")])
    credentials = Mock(side_effect=AssertionError("認証取得は禁止"))
    monkeypatch.setattr(runner.subprocess, "run", credentials)
    reserve = Mock()
    monkeypatch.setattr(runner.Ledger, "reserve", reserve)
    with pytest.raises(Refused, match="^scanは観測用roleのみ$"):
        runner.main()
    reserve.assert_not_called()
    credentials.assert_not_called()


def test_missing_certifi_is_fixed_refusal(monkeypatch):
    import sys
    from scripts.capacity_trial.guard import NEON_HOST
    monkeypatch.setitem(sys.modules, "certifi", None)
    with pytest.raises(Refused) as captured:
        validate_neon(f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb?sslmode=require")
    assert captured.value.code == "credential_unavailable"


def test_concurrency_starts_twelve_real_processes_with_stdin_barrier(ledger, monkeypatch):
    import subprocess
    import sys
    from scripts.capacity_trial import concurrency as module
    original = subprocess.Popen
    def synthetic_child(command, **kwargs):
        # 実Firestoreの代わりにIPCだけを実プロセスで照合する。
        code = ('import json,os,sys; '
                'assert sys.stdin.readline().strip()=="synthetic-token"; '
                'print(json.dumps({"ready":os.getpid()}),flush=True); '
                'assert sys.stdin.readline()=="go\\n"; '
                'print(json.dumps({"pid":os.getpid(),"claim":None}))')
        assert "synthetic-token" not in str(command)
        return original([sys.executable, "-s", "-c", code], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", synthetic_child)
    result = module.run_claimants("trial-concurrency-1", "synthetic-token", ledger)
    assert len(result) == len({row["pid"] for row in result}) == 12


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS専用runnerの孤児回収検証")
@pytest.mark.parametrize("parent_exit", [False, True])
def test_outer_stops_actual_grandchild_before_reaping(tmp_path, parent_exit):
    import os
    import sys
    import subprocess
    from scripts.capacity_trial import __main__ as runner
    pid_file = tmp_path / "grandchild.pid"
    code = ("import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
            "open(sys.argv[1],'w').write(str(p.pid)); "
            + ("sys.exit(2)" if parent_exit else "time.sleep(60)"))
    command = [sys.executable, "-c", code, str(pid_file)]
    if parent_exit:
        result = runner.isolated_run(command, cwd=tmp_path, env=dict(os.environ), input="", timeout=2)
        assert result.returncode == 2
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            runner.isolated_run(command, cwd=tmp_path, env=dict(os.environ), input="", timeout=0.5)
    pid = int(pid_file.read_text())
    # macOSの孤児回収まで短い猶予を置く。生存していれば失敗。
    import time
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("孫プロセスが残っています")


def test_concurrency_rejects_used_scope_before_writing():
    from scripts.capacity_trial.concurrency import concurrency
    store = Mock()
    store.scope_ref.get.return_value.exists = True
    with pytest.raises(Refused, match="競合scopeは使用済み"):
        concurrency(store, "synthetic", Mock())
    store.initialize.assert_not_called()


@pytest.mark.parametrize("operation", ["initialize", "enqueue", "claim", "finish", "recover"])
def test_response_loss_verifies_committed_state_without_replaying(operation):
    from src.sync_capacity.domain import submission
    from scripts.capacity_trial.response_loss import response_loss
    store = Mock()
    store.settings.scope = "trial-loss-" + operation
    item = submission("notion", {"id": "synthetic-response-loss"}, None, 1)
    claim = Mock(owner="synthetic-loss-owner")
    states = {"enqueue": "pending", "claim": "processing", "finish": "completed", "recover": "needs_attention"}
    slots = {"0": None, "1": None, "2": None}
    if operation == "claim":
        slots["0"] = {"job_id": item.job_id, "owner": "synthetic-loss-owner"}
    store.scope_ref.get.return_value.exists = False
    store.scope_ref.get.return_value.to_dict.return_value = {"limit": 3, "slots": slots}
    doc = Mock(id=item.job_id)
    doc.to_dict.return_value = {"state": states.get(operation), "slot": "0"}
    store.jobs.limit.return_value.stream.side_effect = [iter([]), iter([] if operation == "initialize" else [doc])]
    raw = Mock()
    store.client._firestore_api_internal = raw
    def commit(*args, **kwargs):
        store.client._firestore_api_internal.commit(request={})
        return claim
    for name in ("initialize", "enqueue", "claim", "finish", "recover"):
        getattr(store, name).side_effect = commit
    result = response_loss(store)
    assert result["commit_calls"] == result["committed"] == 1
    assert result["saved_state_verified"]
    assert store.client._firestore_api_internal is raw


def test_response_loss_does_not_mislabel_an_actual_rpc_failure():
    from scripts.capacity_trial.response_loss import DropCommitResponse, LostResponse
    raw = Mock()
    raw.commit.side_effect = TimeoutError("実RPCの結果不明")
    drop = DropCommitResponse(raw)
    with pytest.raises(TimeoutError) as captured:
        drop.commit(request={})
    assert not isinstance(captured.value, LostResponse)
    assert drop.calls == 1 and drop.committed == 0


@pytest.fixture
def upper_ledger(tmp_path):
    import time
    now = time.time()
    item = Ledger.initialize(tmp_path / "upper.json", now)
    item.activate_upper_bound("合成未使用DB・Free・追加資源なし証跡", confirmed=True)
    return item


def test_upper_activation_preserves_cost_deadline_and_refuses_reset(upper_ledger):
    before = json.loads(upper_ledger.path.read_text())
    assert before["cost"] is None and before["upper_bound"]["usd"] == 4
    with pytest.raises(Refused):
        upper_ledger.activate_upper_bound("再実行", confirmed=True)
    assert json.loads(upper_ledger.path.read_text()) == before
    upper_ledger.reserve(now=before["created_at"]+7200, reads=1)
    with pytest.raises(Refused):
        upper_ledger.reserve(now=before["created_at"]+14*86400)
    for command in ("scan", "neon-probe"):
        with pytest.raises(Refused):
            upper_ledger.authorize_command(command)


def test_upper_counts_rpc_reads_and_unique_names_atomically(upper_ledger):
    names = [f"synthetic-{n}" for n in range(100)]
    upper_ledger.reserve(reads=4999, rpc=True, document_names=names)
    upper_ledger.reserve(reads=1, rpc=True, document_names=names)
    before = upper_ledger.path.read_bytes()
    for args in ({"reads": 1}, {"document_names": ["synthetic-101"]}, {"sql_connections": 1}):
        with pytest.raises(Refused):
            upper_ledger.reserve(**args)
        assert upper_ledger.path.read_bytes() == before
    upper_ledger.transact(lambda data: data["upper_bound"].update(rpc_reserved=1000))
    with pytest.raises(Refused):
        upper_ledger.reserve(rpc=True)


def test_upper_query_shapes_reject_unbounded_or_arbitrary_filters():
    from google.cloud.firestore_v1.types import StructuredQuery
    from scripts.capacity_trial.small_policy import query_shape
    query = StructuredQuery(from_=[{"collection_id": "jobs"}])
    assert query_shape(query) == 101
    for extra in ({"offset": 1}, {"limit": 1000}, {"select": {"fields": [{"field_path": "event"}]}}):
        with pytest.raises(Refused):
            query_shape(StructuredQuery(from_=[{"collection_id": "jobs"}], **extra))


def test_upper_sdk_create_and_partial_update_are_reserved_and_size_checked(upper_ledger):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore
    from google.cloud.firestore_v1 import _helpers
    from google.cloud.firestore_v1.types import BatchGetDocumentsResponse, Document
    from google.protobuf.timestamp_pb2 import Timestamp
    stamp = Timestamp(seconds=1000)
    name = BASE + "/documents/sync_capacity_scopes/trial-smoke"
    raw = Mock()
    from google.cloud.firestore_v1.types import CommitResponse
    raw.commit.return_value = CommitResponse()
    raw.batch_get_documents.return_value = iter([BatchGetDocumentsResponse(found=Document(
        name=name, fields=_helpers.encode_dict({"version": 1, "limit": 3,
            "slots": {"0": None, "1": None, "2": None}}), update_time=stamp))])
    client = firestore.Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())
    client._firestore_api_internal = GuardedAPI(raw, upper_ledger, role="runner")
    ref = client.document("sync_capacity_scopes/trial-smoke")
    batch = client.batch()
    batch.create(ref, {"version": 1, "limit": 3, "slots": {"0": None, "1": None, "2": None}})
    batch.commit()
    batch = client.batch()
    batch.update(ref, {"version": 1}, option=firestore.LastUpdateOption(stamp))
    batch.commit()
    data = json.loads(upper_ledger.path.read_text())
    assert data["upper_bound"]["document_names"] == [name]
    assert data["upper_bound"]["rpc_reserved"] == 3
    assert data["reserved"]["reads"] == 1 and data["reserved"]["writes"] == 2
    batch = client.batch()
    batch.create(client.document("sync_capacity_scopes/trial-smoke/jobs/too-big"), {"event": "x"*4096})
    with pytest.raises(Refused):
        batch.commit()
    assert raw.commit.call_count == 2


@pytest.mark.parametrize("denied", [True, False])
def test_permission_probe_reaches_server_and_halts_if_write_succeeds(upper_ledger, denied):
    from google.api_core.exceptions import PermissionDenied
    from scripts.capacity_trial.permission import probe
    raw = Mock()
    raw.commit.side_effect = PermissionDenied("合成IAM拒否") if denied else None
    store = Mock()
    store.client._firestore_api_internal = GuardedAPI(raw, upper_ledger, role="observer")
    store.client.document.return_value.get.return_value.exists = True
    store.client.document.return_value.get.return_value.to_dict.return_value = {"event": submission("notion", {"id": "synthetic-smoke"}, None, 0).event}
    if denied:
        assert probe(store, upper_ledger)["server_code"] == 403
        upper_ledger.reserve()
    else:
        with pytest.raises(Refused, match="以後停止"):
            probe(store, upper_ledger)
        with pytest.raises(Refused):
            upper_ledger.reserve()
    raw.commit.assert_called_once()
    data = json.loads(upper_ledger.path.read_text())
    assert data["upper_bound"]["rpc_reserved"] == 1
    assert data["upper_bound"]["document_names"] == [
        BASE+"/documents/sync_capacity_scopes/trial-permission/jobs/synthetic-write-denied"]


def test_upper_all_actual_sdk_query_shapes(upper_ledger):
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter
    raw = Mock()
    raw.run_query.side_effect = lambda **kwargs: iter([])
    client = firestore.Client(project=PROJECT, database=DATABASE, credentials=AnonymousCredentials())
    client._firestore_api_internal = GuardedAPI(raw, upper_ledger, role="runner")
    jobs = client.collection("sync_capacity_scopes").document("trial-smoke").collection("jobs")
    for query in (jobs.limit(1), jobs.select(["state", "created_at"]),
                  jobs.where(filter=FieldFilter("state", "in", ["pending", "retry"]))
                      .order_by("available_at").limit(20)):
        assert list(query.stream()) == []
    data = json.loads(upper_ledger.path.read_text())
    assert data["reserved"]["reads"] == 122
    assert data["upper_bound"]["rpc_reserved"] == 3


def test_upper_partial_update_checks_full_size_and_same_version():
    from google.api_core.exceptions import FailedPrecondition
    from google.cloud.firestore_v1.types import Write, Document
    from google.cloud.firestore_v1 import _helpers
    from google.protobuf.timestamp_pb2 import Timestamp
    from scripts.capacity_trial.small_policy import full_document
    name = BASE+"/documents/sync_capacity_scopes/trial-smoke/jobs/partial"
    stamp = Timestamp(seconds=1000)
    write = Write(update=Document(name=name, fields=_helpers.encode_dict({"state": "completed"})),
                  update_mask={"field_paths": ["state"]}, current_document={"update_time": stamp})
    with pytest.raises(FailedPrecondition):
        full_document(write, lambda name: Document(name=name, update_time=Timestamp(seconds=1001)))
    existing = Document(name=name, fields=_helpers.encode_dict({"event": "x"*4096}), update_time=stamp)
    with pytest.raises(Refused, match="4KiB"):
        full_document(write, lambda name: existing)


def test_permission_unconfirmed_write_halts_future_trials(upper_ledger):
    from google.api_core.exceptions import DeadlineExceeded
    from scripts.capacity_trial.permission import probe
    raw = Mock()
    raw.commit.side_effect = DeadlineExceeded("合成の応答喪失")
    store = Mock()
    store.client._firestore_api_internal = GuardedAPI(raw, upper_ledger, role="observer")
    store.client.document.return_value.get.return_value.exists = True
    store.client.document.return_value.get.return_value.to_dict.return_value = {"event": submission("notion", {"id": "synthetic-smoke"}, None, 0).event}
    with pytest.raises(DeadlineExceeded):
        probe(store, upper_ledger)
    raw.commit.assert_called_once()
    data = json.loads(upper_ledger.path.read_text())
    assert data["halted"].startswith("observer_write_pending:")
    with pytest.raises(Refused):
        upper_ledger.reserve()


def test_permission_forced_exit_leaves_persisted_stop(upper_ledger):
    from scripts.capacity_trial.permission import probe
    raw = Mock()
    def interrupted(**kwargs):
        assert json.loads(upper_ledger.path.read_text())["halted"].startswith("observer_write_pending:")
        raise SystemExit("合成の強制終了")
    raw.commit.side_effect = interrupted
    store = Mock()
    store.client._firestore_api_internal = GuardedAPI(raw, upper_ledger, role="observer")
    store.client.document.return_value.get.return_value.exists = True
    store.client.document.return_value.get.return_value.to_dict.return_value = {"event": submission("notion", {"id": "synthetic-smoke"}, None, 0).event}
    with pytest.raises(SystemExit):
        probe(store, upper_ledger)
    with pytest.raises(Refused):
        upper_ledger.reserve()


def test_permission_local_refusal_does_not_claim_remote_uncertainty(upper_ledger):
    from scripts.capacity_trial.permission import probe
    raw = Mock()
    store = Mock()
    store.client._firestore_api_internal = GuardedAPI(raw, upper_ledger, role="observer")
    store.client.document.return_value.get.return_value.exists = True
    store.client.document.return_value.get.return_value.to_dict.return_value = {"event": submission("notion", {"id": "synthetic-smoke"}, None, 0).event}
    upper_ledger.transact(lambda data: data["upper_bound"].update(rpc_reserved=1000))
    with pytest.raises(Refused):
        probe(store, upper_ledger)
    raw.commit.assert_not_called()
    assert "halted" not in json.loads(upper_ledger.path.read_text())


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOSの終了済group検証")
def test_outer_normal_exit_without_live_group(tmp_path):
    import os,sys
    from scripts.capacity_trial import __main__ as runner
    result = runner.isolated_run([sys.executable, "-c", "print('synthetic')"],
                                cwd=tmp_path, env=dict(os.environ), input="", timeout=2)
    assert result.returncode == 0 and result.stdout == "synthetic\n"


def test_dns_resolver_cannot_be_overridden(tmp_path):
    env = {"HOME": str(tmp_path), "CAPACITY_TRIAL_CHILD": "1", "PYTHONNOUSERSITE": "1", "GRPC_DNS_RESOLVER": "native"}
    validate_environment(env)
    env["GRPC_DNS_RESOLVER"] = "ares"
    with pytest.raises(Refused):
        validate_environment(env)


def test_inspect_state_omits_payload_and_keeps_failure_evidence():
    from scripts.capacity_trial.__main__ import inspect_state
    store = Mock()
    store.scope_ref.get.return_value.exists = True
    store.scope_ref.get.return_value.to_dict.return_value = {"limit": 3, "slots": {"0": None}}
    doc = Mock(id="synthetic-job")
    doc.to_dict.return_value = {"state": "processing", "owner": "synthetic-owner", "attempts": 1,
                               "event": {"body": "合成本文"}, "payload_hash": "合成hash"}
    store.jobs.limit.return_value.stream.return_value = iter([doc])
    result = inspect_state(store)
    assert result["job_count"] == 1 and result["jobs"] == [
        {"id": "synthetic-job", "state": "processing", "owner": "synthetic-owner", "attempts": 1}]
    store.jobs.limit.assert_called_once_with(101)
    store.initialize.assert_not_called()
    store.recover.assert_not_called()


def test_inspect_state_rejects_truncated_result_without_small_guard():
    from scripts.capacity_trial.__main__ import inspect_state
    store = Mock()
    store.jobs.limit.return_value.stream.return_value = iter([Mock()] * 101)
    with pytest.raises(Refused, match="部分結果"):
        inspect_state(store)


def test_concurrency_failure_records_only_fixed_classification(ledger, monkeypatch):
    import subprocess,sys
    from scripts.capacity_trial import concurrency as module
    original = subprocess.Popen
    def failing_child(command, **kwargs):
        code = ('import json,os,sys;sys.stdin.readline();'
                'print(json.dumps({"ready":os.getpid()}),flush=True);sys.stdin.readline();'
                'print("SDK synthetic-not-for-ledger",file=sys.stderr);'
                'print("CAPACITY_TRIAL_FAILURE_V1 "+json.dumps({"state":"failed","partial_result":False,"error_type":"FailedPrecondition","error_code":"unexpected_error"}),file=sys.stderr);sys.exit(1)')
        return original([sys.executable, "-s", "-c", code], **kwargs)
    monkeypatch.setattr(module.subprocess,"Popen",failing_child)
    with pytest.raises(Refused):
        module.run_claimants("trial-concurrency-2", "synthetic-token", ledger)
    raw = ledger.path.read_text()
    entry = json.loads(raw)["claimant_failures"][0]
    assert len(entry["failures"]) == 12 and entry["successful_children"] == 0
    assert all(item["error_type"] == "FailedPrecondition" for item in entry["failures"])
    assert "synthetic-not-for-ledger" not in raw


@pytest.mark.skipif(__import__("sys").platform != "darwin", reason="macOS専用launcher検証")
def test_outer_early_exit_preserves_child_error(tmp_path):
    import os,sys,json
    from scripts.capacity_trial.__main__ import isolated_run
    result = isolated_run([sys.executable, "-c",
        "import sys,json;sys.stdin.close();print(json.dumps({'error_code':'environment_rejected'}),file=sys.stderr);sys.exit(1)"],
        cwd=tmp_path, env=dict(os.environ), input="synthetic"*10000, timeout=2)
    assert result.returncode == 1 and json.loads(result.stderr)["error_code"] == "environment_rejected"


def test_stage_transition_keeps_original_usage_and_deadline(upper_ledger):
    upper_ledger.reserve(reads=1728, writes=242, runtime_seconds=2905, rpc=True,
                         document_names=["synthetic-a"])
    before = json.loads(upper_ledger.path.read_text())
    upper_ledger.advance_upper_bound("合成の再照合証跡", confirmed=True)
    after = json.loads(upper_ledger.path.read_text())
    for key in ("created_at", "reserved", "rpc_calls", "returned_documents", "cost"):
        assert after[key] == before[key]
    assert after["upper_bound_transition"]["previous"] == before["upper_bound"]
    assert after["upper_bound"]["rpc_reserved"] == 1
    assert after["upper_bound"]["document_names"] == ["synthetic-a"]
    assert after["upper_bound"]["usd"] == 8
    with pytest.raises(Refused):
        upper_ledger.advance_upper_bound("再実行", confirmed=True)
    with pytest.raises(Refused):
        upper_ledger.reserve(now=before["created_at"]+14*86400)
    for command in ("scan", "neon-probe"):
        with pytest.raises(Refused):
            upper_ledger.authorize_command(command)


@pytest.mark.parametrize("change", [
    {"halted": "synthetic-halt"}, {"created_at": 0},
    {"cost": {"usd": 2}}, {"upper_bound": None},
])
def test_stage_transition_refusal_does_not_mutate(upper_ledger, change):
    upper_ledger.transact(lambda data: data.update(change))
    before = upper_ledger.path.read_bytes()
    with pytest.raises(Refused):
        upper_ledger.advance_upper_bound("合成", confirmed=True)
    assert upper_ledger.path.read_bytes() == before


def test_stage_two_limits_still_fail_closed(upper_ledger):
    upper_ledger.advance_upper_bound("合成", confirmed=True)
    upper_ledger.reserve(reads=10000, document_names=[f"synthetic-{n}" for n in range(100)])
    upper_ledger.transact(lambda data: data["upper_bound"].update(rpc_reserved=2000))
    before = upper_ledger.path.read_bytes()
    for args in ({"reads": 1}, {"rpc": True}, {"document_names": ["synthetic-extra"]},
                 {"deletes": 1}, {"sql_connections": 1}):
        with pytest.raises(Refused):
            upper_ledger.reserve(**args)
        assert upper_ledger.path.read_bytes() == before


def test_concurrency_preparation_failure_leaves_fixed_phase(ledger, monkeypatch):
    import subprocess, sys
    from scripts.capacity_trial import concurrency as module
    original = subprocess.Popen
    def bad_ready(command, **kwargs):
        return original([sys.executable, "-s", "-c",
            "import sys;sys.stdin.readline();print('synthetic-secret',flush=True)"], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", bad_ready)
    with pytest.raises(ValueError):
        module.run_claimants("trial-concurrency-1", "synthetic-token", ledger)
    raw = ledger.path.read_text()
    entry = json.loads(raw)["claimant_run_errors"][0]
    assert entry["phase"] == "preparing" and entry["error_type"] == "JSONDecodeError"
    assert entry["children_stop_verified_at_error"] is False
    assert "synthetic-secret" not in raw


def test_stage_two_stops_when_recorded_cost_reaches_two(upper_ledger):
    import time
    upper_ledger.advance_upper_bound("合成", confirmed=True)
    upper_ledger.cost(2, time.time(), "合成費用")
    before = upper_ledger.path.read_bytes()
    with pytest.raises(Refused):
        upper_ledger.reserve(rpc=True)
    assert upper_ledger.path.read_bytes() == before
