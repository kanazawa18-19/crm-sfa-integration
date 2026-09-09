"""実サービスへ接続せず、接続前拒否と予算停止を検証する。"""
import json
from unittest.mock import Mock

import pytest

from scripts.capacity_trial.guard import (
    BASE, PROJECT, Ledger, Refused, validate_environment, validate_neon,
    validate_resource, validate_target,
)
from scripts.capacity_trial.firestore import GuardedAPI


@pytest.fixture
def ledger(tmp_path):
    item = Ledger.initialize(tmp_path / "ledger.json", 1000, now=1000)
    item.cost(0.1, 1000, "合成ローカルテスト値", now=1000)
    return item


@pytest.mark.parametrize("project,database,scope,slots,role", [
    ("fabled-electron-406310", "(default)", "trial-smoke", 3, "runner"),
    (PROJECT, "capacity-restore", "trial-smoke", 3, "runner"),
    (PROJECT, "(default)", "production", 3, "runner"),
    (PROJECT, "(default)", "trial-arbitrary", 3, "runner"),
    (PROJECT, "(default)", "trial-smoke", 4, "runner"),
    (PROJECT, "(default)", "trial-smoke", 3, "owner"),
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
    client = firestore.Client(project=PROJECT, credentials=AnonymousCredentials())
    client._firestore_api_internal = GuardedAPI(raw, Mock(), role="observer")
    store = FirestoreJobStore(Settings(PROJECT, "(default)", "trial-smoke", 3), client=client)
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


def test_neon_probe_checks_two_sessions_and_closes(monkeypatch):
    from unittest.mock import MagicMock
    from scripts.capacity_trial import neon
    from scripts.capacity_trial.guard import NEON_HOST
    monkeypatch.setattr(neon, "validate_environment", lambda: None)
    first, second = MagicMock(), MagicMock()
    for connection in [first, second]:
        connection.__enter__.return_value = connection
    first.execute.return_value.fetchone.side_effect = [("neondb", "neondb_owner", 1), (True,), (True,)]
    second.execute.return_value.fetchone.side_effect = [("neondb", "neondb_owner", 2), (False,), (True,), (True,)]
    connector = Mock(side_effect=[first, second])
    monkeypatch.setattr("psycopg.connect", connector)
    result = neon.probe(f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb?sslmode=require", Mock())
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
    responses = iter([
        subprocess.CompletedProcess([], 0, stdout="synthetic-secret", stderr=""),
        subprocess.CompletedProcess([], 1, stdout="", stderr=json.dumps({
            "state": "failed", "error_code": child_code, "message": "synthetic-secret"})),
    ])
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: next(responses))
    with pytest.raises(Refused) as captured:
        runner.main()
    assert captured.value.code == expected
    assert "synthetic-secret" not in str(captured.value)


def test_neon_missing_trusted_ca_bundle_is_rejected(tmp_path, monkeypatch):
    from scripts.capacity_trial.guard import NEON_HOST
    monkeypatch.setattr("certifi.where", lambda: str(tmp_path / "missing-ca.pem"))
    with pytest.raises(Refused):
        validate_neon(f"postgresql://neondb_owner:synthetic@{NEON_HOST}/neondb?sslmode=require")
