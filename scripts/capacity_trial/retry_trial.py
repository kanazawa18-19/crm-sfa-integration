"""認証より前に開始を消費する、固定4job試験専用の隔離起動。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from .guard import ACCOUNTS, DATABASE, PROJECT, Refused, validate_environment
from . import retry_policy as policy


def validate_diagnostics(value):
    from .diagnostics import ORIGINS
    if not isinstance(value, list) or not 1 <= len(value) <= 5:
        raise Refused("成功診断が欠測または不正")
    for item in value:
        if (not isinstance(item, dict) or set(item) != {"attempts", "retries", "elapsed_ms", "outcome", "phase", "conflicts"}
                or type(item["attempts"]) is not int or not 1 <= item["attempts"] <= 8
                or type(item["retries"]) is not int or item["retries"] != item["attempts"] - 1
                or type(item["elapsed_ms"]) is not int or not 0 <= item["elapsed_ms"] < 30000
                or item["outcome"] != "success" or item["phase"] != "commit"
                or not isinstance(item["conflicts"], list) or len(item["conflicts"]) != item["retries"]):
            raise Refused("成功診断が欠測または不正")
        for index, event in enumerate(item["conflicts"], 1):
            if (not isinstance(event, dict) or set(event) != {"attempt", "elapsed_ms", "phase", "definite", "origin"}
                    or type(event["attempt"]) is not int or event["attempt"] != index
                    or type(event["elapsed_ms"]) is not int or not 0 <= event["elapsed_ms"] <= item["elapsed_ms"]
                    or event["phase"] not in {"operation", "commit"}
                    or event["definite"] is not True or event["origin"] not in ORIGINS):
                raise Refused("競合診断が不正")


def verify_observation(result):
    scope = result.get("scope") or {}
    jobs = result.get("jobs") or []
    if (result.get("scope_exists") is not True or result.get("job_count") != 4 or len(jobs) != 4
            or len({job["id"] for job in jobs}) != 4 or scope.get("limit") != 3
            or set(scope.get("slots", {})) != {"0", "1", "2"}
            or sum(job.get("state") == "pending" for job in jobs) != 1
            or sum(job.get("state") == "processing" for job in jobs) != 3):
        raise Refused("事後の4job・3枠照合失敗")
    for job in jobs:
        if job["state"] == "processing":
            if (job.get("attempts") != 1 or not job.get("owner")
                    or scope["slots"].get(job.get("slot")) != {"job_id": job["id"], "owner": job["owner"]}):
                raise Refused("事後の所有者・枠照合失敗")
        elif job.get("attempts") != 0 or job.get("owner") or job.get("slot"):
            raise Refused("待機jobが未取得状態ではありません")


def main(args, ledger):
    from .__main__ import isolated_run, inspect_state
    from .concurrency import claim_child, retry_concurrency
    role = "observer" if args.command == "retry-observe" else "runner"
    if args.scope != policy.SCOPE or args.role != role or args.command not in {"retry-deadline", "retry-observe", "claim-child"}:
        raise Refused("固定試験のコマンド・scope・roleが不正")
    policy.path_check(ledger)
    if not os.environ.get("CAPACITY_TRIAL_CHILD"):
        if args.command == "claim-child":
            raise Refused("限定試験の子は調整役からだけ起動できます")
        ticket = policy.begin(ledger, role)
        stopped = True
        result = None
        failure = None
        try:
            ledger.reserve()
            token = subprocess.run(["/usr/bin/security", "find-generic-password", "-s",
                "capacity-trial-token-" + role, "-a", ACCOUNTS[role], "-w"],
                capture_output=True, text=True, check=False)
            if token.returncode or not token.stdout.strip():
                raise Refused("専用キーチェーン認証情報が取得できません")
            with tempfile.TemporaryDirectory(prefix="capacity-retry-home-") as home:
                env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONNOUSERSITE": "1",
                       "CAPACITY_TRIAL_CHILD": "1", "GRPC_DNS_RESOLVER": "native"}
                stopped = False
                try:
                    child = isolated_run([sys.executable, "-s", "-m", "scripts.capacity_trial", *sys.argv[1:]],
                        cwd=Path(__file__).resolve().parents[2], env=env,
                        input=json.dumps({"token": token.stdout.strip(), "ticket": ticket}), timeout=60)
                except subprocess.TimeoutExpired:
                    stopped = True
                    raise Refused("限定試験の外側60秒期限超過", code="trial_timeout") from None
                stopped = True
                if child.returncode:
                    from .diagnostics import parse_failure
                    failure = parse_failure(child.stderr)
                    raise Refused("限定試験子が失敗", code=failure["error_code"])
                result = json.loads(child.stdout)
                return result
        except Exception as exc:
            if failure is None:
                from .diagnostics import failure_record
                failure = failure_record(exc)
            raise
        finally:
            def finish(data):
                part = data["retry_trial"][role]
                part["finished_at"] = time.time()
                part["passed"] = result is not None and result.get("state") == "passed"
                if failure is not None:
                    part["failure"] = failure
                if role == "runner" and result is not None:
                    part["retained_state"] = result["result"]["retained_state"]
                if role == "runner":
                    data["retry_trial"]["children_stopped"] = stopped
            ledger.transact(finish)
    validate_environment()
    payload = json.loads(sys.stdin.readline() if args.command == "claim-child" else sys.stdin.read())
    if not isinstance(payload, dict) or set(payload) != {"token", "ticket"}:
        raise Refused("限定試験の起動情報が不正")
    policy.attach(ledger, role, payload["ticket"], child=args.command == "claim-child")
    if args.command == "claim-child":
        return claim_child(args.scope, ledger, token=payload["token"])
    ledger.reserve(runtime_seconds=60)
    from .firestore import make_store
    started = time.time()
    store = make_store(args.scope, payload["token"], ledger, role=role)
    if role == "runner":
        result = retry_concurrency(store, payload["token"], ledger)
    else:
        result = inspect_state(store)
        verify_observation(result)
        expected = ledger.transact(lambda data: data["retry_trial"]["runner"].get("retained_state"))
        actual = {"slots": result["scope"]["slots"], "jobs": {job["id"]: {
            key: job.get(key) for key in ("state", "owner", "slot", "attempts")} for job in result["jobs"]}}
        if expected is not None and actual != expected:
            raise Refused("試験と事後観測の4job・所有者・枠が不一致")
        result["trial_comparison"] = "matched" if expected is not None else "runner_result_unavailable"
    state = "observed" if role == "observer" and result["trial_comparison"] != "matched" else "passed"
    return {"state": state, "case": args.command, "project": PROJECT, "database": DATABASE,
            "scope": args.scope, "started_at": started, "finished_at": time.time(), "result": result}
