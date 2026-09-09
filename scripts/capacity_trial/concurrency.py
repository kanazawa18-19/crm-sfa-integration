"""12独立プロセスを同期開始し、解放せず3枠の保持状態を再読する。"""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

from .guard import ERROR_CODES, Ledger, Refused, validate_environment


def claim_child(scope, ledger):
    from dataclasses import asdict
    from .firestore import make_store
    validate_environment()
    token = sys.stdin.readline().strip()
    store = make_store(scope, token, ledger)
    ledger.reserve(runtime_seconds=50)
    print(json.dumps({"ready": os.getpid()}), flush=True)
    if sys.stdin.readline() != "go\n":
        raise Refused("競合開始合図が不正")
    claim = store.claim(f"synthetic-process-{os.getpid()}", time.time())
    return {"pid": os.getpid(), "claim": asdict(claim) if claim else None}


def run_claimants(scope, token, ledger):
    """秘密はstdinだけに流す。終了確認できない子があれば成功を返さない。"""
    deadline = time.monotonic() + 45
    processes, homes = [], []
    try:
        for _ in range(12):
            home = tempfile.TemporaryDirectory(prefix="capacity-claim-home-")
            homes.append(home)
            env = {"HOME": home.name, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                   "PYTHONNOUSERSITE": "1", "CAPACITY_TRIAL_CHILD": "1", "GRPC_DNS_RESOLVER": "native"}
            process = subprocess.Popen([sys.executable, "-s", "-m", "scripts.capacity_trial",
                "claim-child", "--ledger", str(ledger.path), "--scope", scope],
                cwd=Path(__file__).resolve().parents[2], env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            processes.append(process)
            process.stdin.write(token + "\n")
            process.stdin.flush()
        with selectors.DefaultSelector() as selector:
            for process in processes:
                selector.register(process.stdout, selectors.EVENT_READ, process)
            while selector.get_map():
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise Refused("競合子の準備期限超過", code="trial_timeout")
                for key, _ in selector.select(remaining):
                    ready = json.loads(key.fileobj.readline())
                    if ready != {"ready": key.data.pid}:
                        raise Refused("競合子の準備応答が不正")
                    selector.unregister(key.fileobj)
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.close()
            process.stdin = None
        results, failures = [], []
        for process in processes:
            try:
                output, error_output = process.communicate(timeout=max(0.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                raise Refused("競合子の終了期限超過", code="trial_timeout") from None
            if process.returncode:
                try:
                    error = json.loads(error_output)
                except (ValueError, TypeError):
                    error = {}
                if not isinstance(error, dict):
                    error = {}
                kinds = {"Refused", "FailedPrecondition", "Aborted", "AlreadyExists", "CapacityUnavailable",
                         "ServiceUnavailable", "DeadlineExceeded", "PermissionDenied", "AssertionError"}
                failures.append({"pid": process.pid, "return_code": process.returncode,
                    "error_type": error.get("error_type") if error.get("error_type") in kinds else "unknown",
                    "error_code": error.get("error_code") if error.get("error_code") in ERROR_CODES else "unexpected_error"})
                continue
            result = json.loads(output)
            if result.get("pid") != process.pid:
                raise Refused("競合子のPID照合失敗")
            result["return_code"] = process.returncode
            results.append(result)
        if failures:
            # 例外本文を残さず、全子を待ち終えた固定分類だけを台帳へ保存する。
            ledger.transact(lambda data: data.setdefault("claimant_failures", []).append(
                {"scope": scope, "failures": failures, "successful_children": len(results)}))
            raise Refused("競合子が失敗", code="child_failed")
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.wait()
        for home in homes:
            home.cleanup()


def concurrency(store, token, ledger):
    from src.sync_capacity.domain import submission
    # 使用済みscopeを再利用すると過去の結果を今回の成果に混ぜるため拒否する。
    if store.scope_ref.get().exists or list(store.jobs.limit(1).stream()):
        raise Refused("競合scopeは使用済み")
    store.initialize(apply=True)
    now = time.time()
    for index in range(12):
        store.enqueue(submission("notion", {"id": f"synthetic-concurrency-{index}"}, None, now), now)
    results = run_claimants(store.settings.scope, token, ledger)
    claims = [item["claim"] for item in results if item["claim"] is not None]
    if (len(results) != 12 or len({item["pid"] for item in results}) != 12
            or len(claims) != 3 or len({item["job_id"] for item in claims}) != 3
            or len({item["slot"] for item in claims}) != 3):
        raise AssertionError("12独立process・3枠・重複なしの照合失敗")
    scope = store.scope_ref.get().to_dict()
    jobs = {doc.id: doc.to_dict() for doc in store.jobs.limit(13).stream()}
    if (len(jobs) != 12 or sum(job["state"] == "processing" for job in jobs.values()) != 3
            or sum(job["state"] == "pending" for job in jobs.values()) != 9
            or scope["limit"] != 3 or set(scope["slots"]) != {"0", "1", "2"}):
        raise AssertionError("保存された12件・処理中3件の照合失敗")
    for claim in claims:
        job = jobs[claim["job_id"]]
        if (job["state"] != "processing" or job["attempts"] != 1
                or job["owner"] != claim["owner"] or job["slot"] != claim["slot"]
                or scope["slots"][claim["slot"]] != {"job_id": claim["job_id"], "owner": claim["owner"]}):
            raise AssertionError("jobと枠の所有者照合失敗")
    return {"process_count": len(results), "claimed_count": len(claims), "claims": claims,
            "pids": [item["pid"] for item in results], "documents": len(jobs),
            "children_stopped": all(item["return_code"] == 0 for item in results), "slots_retained": True}
