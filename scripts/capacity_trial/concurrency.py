"""12独立プロセスを同期開始し、解放せず3枠の保持状態を再読する。"""
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time

from .guard import Ledger, Refused, validate_environment


def claim_child(scope, ledger, token=None):
    from dataclasses import asdict
    from .firestore import make_store
    validate_environment()
    token = sys.stdin.readline().strip() if token is None else token
    store = make_store(scope, token, ledger)
    ledger.reserve(runtime_seconds=50)
    print(json.dumps({"ready": os.getpid()}), flush=True)
    if sys.stdin.readline() != "go\n":
        raise Refused("競合開始合図が不正")
    if scope == "trial-retry-deadline-1":
        store.atomic_diagnostics = []
    claim_started = time.monotonic()
    claim = store.claim(f"synthetic-process-{os.getpid()}", time.time())
    if scope == "trial-retry-deadline-1" and time.monotonic() - claim_started >= 30:
        raise Refused("限定試験の製品30秒期限超過", code="trial_timeout")
    result = {"pid": os.getpid(), "claim": asdict(claim) if claim else None}
    if scope == "trial-retry-deadline-1":
        result["diagnostics"] = store.atomic_diagnostics
    return result


def run_claimants(scope, token, ledger):
    """秘密はstdinだけに流す。終了確認できない子があれば成功を返さない。"""
    from src.sync_capacity.deadline import current_deadline
    active = current_deadline.get()
    deadline = min(time.monotonic() + 45, active.expires_at if active else float("inf"))
    processes, homes = [], []
    phase = "starting"
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
            payload = (json.dumps({"token": token, "ticket": ledger.retry_ticket})
                       if scope == "trial-retry-deadline-1" else token)
            process.stdin.write(payload + "\n")
            process.stdin.flush()
        phase = "preparing"
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
        phase = "signaling"
        for process in processes:
            process.stdin.write("go\n")
            process.stdin.close()
            process.stdin = None
        phase = "collecting"
        results, failures = [], []
        for process in processes:
            try:
                output, error_output = process.communicate(timeout=max(0.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                raise Refused("競合子の終了期限超過", code="trial_timeout") from None
            if process.returncode:
                from .diagnostics import parse_failure
                failures.append({"pid": process.pid, "return_code": process.returncode,
                                 **parse_failure(error_output)})
                continue
            result = json.loads(output)
            if result.get("pid") != process.pid:
                raise Refused("競合子のPID照合失敗")
            result["return_code"] = process.returncode
            if scope == "trial-retry-deadline-1":
                from .retry_trial import validate_diagnostics
                validate_diagnostics(result.get("diagnostics"))
            results.append(result)
        if failures:
            # 例外本文を残さず、全子を待ち終えた固定分類だけを台帳へ保存する。
            ledger.transact(lambda data: data.setdefault("claimant_failures", []).append(
                {"scope": scope, "failures": failures, "successful_children": len(results)}))
            raise Refused("競合子が失敗", code="child_failed")
        if time.monotonic() >= deadline:
            raise Refused("競合子の終了期限超過", code="trial_timeout")
        return results
    except Exception as exc:
        # 準備前終了や不正JSONでも、本文を捨てた固定の段階・型だけを残す。
        kinds = {"Refused", "JSONDecodeError", "BrokenPipeError", "TimeoutExpired"}
        ledger.transact(lambda data: data.setdefault("claimant_run_errors", []).append({
            "scope": scope, "phase": phase, "started_children": len(processes),
            "error_type": type(exc).__name__ if type(exc).__name__ in kinds else "unknown",
            "error_code": exc.code if isinstance(exc, Refused) else "unexpected_error",
            "children_stop_verified_at_error": False}))
        raise
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
        for process in processes:
            process.wait()
        for home in homes:
            home.cleanup()


def concurrency(store, token, ledger):
    return _concurrency(store, token, ledger, 12)


def retry_concurrency(store, token, ledger):
    if store.settings.scope != "trial-retry-deadline-1":
        raise Refused("固定4job試験のscopeが不正")
    from src.sync_capacity.deadline import Deadline, using_deadline
    deadline = Deadline.after(45)
    with using_deadline(deadline):
        result = _concurrency(store, token, ledger, 4)
        deadline.require()
        return result


def _concurrency(store, token, ledger, job_count):
    from src.sync_capacity.domain import submission
    # 使用済みscopeを再利用すると過去の結果を今回の成果に混ぜるため拒否する。
    if store.scope_ref.get().exists or list(store.jobs.limit(1).stream()):
        raise Refused("競合scopeは使用済み")
    def product_call(operation, *args, **kwargs):
        if job_count == 12:
            return operation(*args, **kwargs)
        from src.sync_capacity.deadline import Deadline, using_deadline
        with using_deadline(Deadline.after(30)) as deadline:
            result = operation(*args, **kwargs, deadline=deadline)
            deadline.require()
            return result
    product_call(store.initialize, apply=True)
    now = time.time()
    expected_ids = []
    for index in range(job_count):
        request = submission("notion", {"id": f"synthetic-concurrency-{index}"}, None, now)
        expected_ids.append(request.job_id)
        product_call(store.enqueue, request, now)
    results = run_claimants(store.settings.scope, token, ledger)
    claims = [item["claim"] for item in results if item["claim"] is not None]
    if (len(results) != 12 or len({item["pid"] for item in results}) != 12
            or len(claims) != 3 or len({item["job_id"] for item in claims}) != 3
            or len({item["slot"] for item in claims}) != 3):
        raise AssertionError("12独立process・3枠・重複なしの照合失敗")
    scope = store.scope_ref.get().to_dict()
    jobs = {doc.id: doc.to_dict() for doc in store.jobs.limit(13).stream()}
    if (set(jobs) != set(expected_ids) or len(jobs) != job_count or sum(job["state"] == "processing" for job in jobs.values()) != 3
            or sum(job["state"] == "pending" for job in jobs.values()) != job_count - 3
            or scope["limit"] != 3 or set(scope["slots"]) != {"0", "1", "2"}):
        raise AssertionError("保存件数・処理中3件の照合失敗")
    for claim in claims:
        if claim["owner"] not in {f"synthetic-process-{item['pid']}" for item in results if item["claim"] == claim}:
            raise AssertionError("子PIDと所有者が不一致")
        job = jobs[claim["job_id"]]
        if (job["state"] != "processing" or job["attempts"] != 1
                or job["owner"] != claim["owner"] or job["slot"] != claim["slot"]
                or scope["slots"][claim["slot"]] != {"job_id": claim["job_id"], "owner": claim["owner"]}):
            raise AssertionError("jobと枠の所有者照合失敗")
    if any(job.get("attempts") != 0 or job.get("owner") or job.get("slot")
           for job in jobs.values() if job["state"] == "pending"):
        raise AssertionError("待機jobの所有者・試行回数が不正")
    result = {"process_count": len(results), "claimed_count": len(claims), "claims": claims,
            "pids": [item["pid"] for item in results], "documents": len(jobs),
            "children_stopped": all(item["return_code"] == 0 for item in results), "slots_retained": True}

    if job_count == 4:
        result["retained_state"] = {"slots": scope["slots"], "jobs": {
            job_id: {key: job.get(key) for key in ("state", "owner", "slot", "attempts")}
            for job_id, job in jobs.items()}}
        result["diagnostics"] = [{"pid": item["pid"], "operations": item["diagnostics"]} for item in results]
        result["retry_observation"] = ("observed" if any(
            operation["outcome"] == "success" and operation["retries"] >= 1
            and any(event["attempt"] == 1 and event["definite"] and event["elapsed_ms"] > 3000
                    for event in operation["conflicts"])
            for item in results for operation in item["diagnostics"]) else "not_observed")
    return result
