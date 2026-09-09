"""明示した小規模ケースだけ実行し、終了時はJSON証跡を返す。"""
from __future__ import annotations

import argparse
import json
import os
import signal
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

from .guard import ACCOUNTS, DATABASE, ERROR_CODES, Ledger, PROJECT, Refused, validate_environment, validate_target


def smoke(store):
    from src.sync_capacity.domain import submission, PayloadConflict
    from src.sync_capacity.application import drain_one
    if list(store.jobs.limit(1).stream()):
        raise Refused("smoke scopeは既に使用済み。過去の成功を再利用しません")
    store.initialize(apply=True)
    now = time.time()
    item = submission("notion", {"id": "synthetic-smoke"}, None, now)
    store.enqueue(item, now)
    store.enqueue(item, now)
    try:
        store.enqueue(submission("notion", {"id": "synthetic-smoke", "value": "changed"}, None, now), now)
    except PayloadConflict:
        pass
    else:
        raise AssertionError("内容不一致が拒否されなかった")
    result = drain_one(store, lambda claim: lambda: ({"statusCode": 200, "body": "{}"}, False))
    if result.get("state") != "completed" or result.get("job_id") != item.job_id:
        raise AssertionError("今回のjob完了が確認できない")
    observed = store.observe()
    if observed["total"] != 1 or observed["counts"]["completed"] != 1:
        raise AssertionError("保存件数・終了状態の不一致")
    return {"worker": result, "observation": observed}


def scan(store):
    # 合成文書投入は別実装。既に合成投入した固定scopeだけを観測する。
    result = store.observe()
    if result["scan_seconds"] > 60:
        raise Refused("走査60秒超過。部分結果を破棄")
    return result


def inspect_state(store):
    scope = store.scope_ref.get()
    documents = list(store.jobs.limit(101).stream())
    if len(documents) > 100:
        raise Refused("保存状態の100文書超過。部分結果を破棄")
    return {"scope_exists": scope.exists, "scope": scope.to_dict(),
              "job_count": len(documents), "jobs": [
                  {"id": doc.id, **{key: value for key, value in doc.to_dict().items()
                   if key in {"state", "owner", "slot", "attempts", "reason"}}}
                  for doc in documents]}


def isolated_run(command, *, cwd, env, input, timeout=60):
    # 未回収のPIDを保持したままgroupを停止し、PID再利用との競合を防ぐ。
    if not all(hasattr(os, name) for name in ("waitid", "P_PID", "WNOWAIT", "WEXITED", "WNOHANG")):
        raise Refused("未回収の終了監視が利用できません")
    with subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as process:
        output = ["", ""]
        def collect(index, stream):
            output[index] = stream.read()
        readers = [threading.Thread(target=collect, args=(index, stream), daemon=True)
                   for index, stream in enumerate((process.stdout, process.stderr))]
        try:
            for reader in readers:
                reader.start()
            process.stdin.write(input)
            process.stdin.close()
            process.stdin = None
            deadline = time.monotonic() + timeout
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, timeout)
                time.sleep(0.02)
        finally:
            # waitidは回収しない。kill前にpoll/wait/communicateを呼ばない。
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                # このMacでは終了済みのみのgroupへのkillpgがEPERMとなった。
                states = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="],
                                        capture_output=True, text=True, timeout=5, check=True)
                members = [line.split() for line in states.stdout.splitlines() if line.strip()]
                if (not any(int(row[0]) == process.pid and int(row[1]) == process.pid
                            and row[2].startswith("Z") for row in members)
                        or any(int(row[1]) == process.pid and not row[2].startswith("Z") for row in members)):
                    raise Refused("試験プロセスグループの停止が拒否されました") from None
            process.wait()
            for reader in readers:
                if reader.ident is not None:
                    reader.join(timeout=5)
            if any(reader.is_alive() for reader in readers):
                raise Refused("試験プロセスの出力終了を確認できません")
        return subprocess.CompletedProcess(command, process.returncode, *output)


def main():
    parser = argparse.ArgumentParser(description="承認済み専用Firestore・Neonの隔離試験。本番同期は対象外")
    parser.add_argument("command", choices=["init-ledger", "activate-upper-bound", "migrate-target", "record-cost", "smoke", "scan", "inspect-state", "neon-probe", "concurrency", "claim-child", "response-loss", "permission-probe"])
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--created-at", type=float)
    parser.add_argument("--usd", type=float)
    parser.add_argument("--observed-at", type=float)
    parser.add_argument("--evidence", default="")
    parser.add_argument("--cost-basis", choices=["metered", "free-plan-verified"], default="metered")
    parser.add_argument("--confirm-unused-database", action="store_true")
    parser.add_argument("--confirm-neon-free", action="store_true")
    parser.add_argument("--confirm-no-extra-resources", action="store_true")
    parser.add_argument("--scope", default="trial-smoke")
    parser.add_argument("--role", choices=ACCOUNTS, default="runner")
    args = parser.parse_args()
    if args.command == "init-ledger":
        if args.created_at is None:
            raise Refused("実資源の作成UTC epochが必要")
        Ledger.initialize(args.ledger, args.created_at)
        return {"state": "ledger_initialized", "cost": "未観測・実行禁止"}
    ledger = Ledger(args.ledger)
    if args.command == "activate-upper-bound":
        ledger.activate_upper_bound(args.evidence, confirmed=(args.confirm_unused_database
            and args.confirm_neon_free and args.confirm_no_extra_resources))
        return {"state": "upper_bound_activated", "basis": "reserved-upper-bound",
                "reserved_usd": 4, "project": PROJECT, "database": DATABASE,
                "limitation": "元の14日期限内の推定上限。実測費用ではない"}
    if args.command == "migrate-target":
        ledger.migrate_target()
        return {"state": "target_migrated", "project": PROJECT, "database": DATABASE, "cost": "再照合待ち・実行禁止"}
    if args.command == "record-cost":
        if args.usd is None or args.observed_at is None:
            raise Refused("費用実測・観測時刻が必要。未観測を0と登録しない")
        ledger.cost(args.usd, args.observed_at, args.evidence, basis=args.cost_basis)
        return {"state": "cost_recorded"}
    validate_target(PROJECT, DATABASE, args.scope, role=args.role)
    if args.command == "smoke" and (args.scope != "trial-smoke" or args.role != "runner"):
        raise Refused("smokeは専用scopeとrunnerのみ")
    if args.command == "inspect-state":
        from .small_policy import SMALL_SCOPES
        if args.role != "observer" or args.scope not in SMALL_SCOPES:
            raise Refused("保存状態確認は小規模scopeとobserverのみ")
    if args.command == "scan" and args.role != "observer":
        raise Refused("scanは観測用roleのみ")
    if args.command in {"concurrency", "claim-child"} and (
            args.role != "runner" or args.scope not in {f"trial-concurrency-{n}" for n in range(1, 6)}):
        raise Refused("競合試験は専用scopeとrunnerのみ")
    if args.command == "response-loss" and (args.role != "runner" or args.scope not in {
            f"trial-loss-{name}" for name in ("initialize", "enqueue", "claim", "finish", "recover")}):
        raise Refused("応答喪失試験は専用scopeとrunnerのみ")
    if args.command == "permission-probe" and (args.role != "observer" or args.scope != "trial-permission"):
        raise Refused("IAM試験はtrial-permissionとobserverのみ")
    ledger.authorize_command(args.command)
    if args.command == "claim-child":
        from .concurrency import claim_child
        return claim_child(args.scope, ledger)
    if not os.environ.get("CAPACITY_TRIAL_CHILD"):
        # 接続情報・認証・Python startup設定は全て切り離す。
        ledger.reserve()
        service = "capacity-trial-neon-dsn" if args.command == "neon-probe" else "capacity-trial-token-" + args.role
        account = "neondb_owner" if args.command == "neon-probe" else ACCOUNTS[args.role]
        token = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service,
                                "-a", account, "-w"],
                               capture_output=True, text=True, check=False)
        if token.returncode or not token.stdout.strip():
            raise Refused("専用キーチェーン認証情報が取得できません")
        with tempfile.TemporaryDirectory(prefix="capacity-trial-home-") as home:
            env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                   "PYTHONNOUSERSITE": "1", "CAPACITY_TRIAL_CHILD": "1", "GRPC_DNS_RESOLVER": "native"}
            try:
                child = isolated_run([sys.executable, "-s", "-m", "scripts.capacity_trial", *sys.argv[1:]],
                    cwd=Path(__file__).resolve().parents[2], env=env, input=token.stdout.strip(),
                    timeout=60)
            except subprocess.TimeoutExpired:
                raise Refused("60秒で子を停止。結果不明の枠は自動回収しません") from None
            if child.returncode:
                code = "child_failed"
                try:
                    error = json.loads(child.stderr)
                    if (isinstance(error, dict) and error.get("state") == "failed"
                            and error.get("error_code") in ERROR_CODES):
                        code = error["error_code"]
                except (ValueError, TypeError):
                    pass
                raise Refused("隔離子プロセス失敗。部分結果は返しません", code=code)
            # 子のJSON以外を出さない。stderrの秘密混入を避ける。
            return json.loads(child.stdout)
    validate_environment()
    ledger.reserve(runtime_seconds=60)
    token = sys.stdin.read().strip()
    started = time.time()
    if args.command == "neon-probe":
        from .neon import probe
        return {"state": "passed", "case": args.command, "started_at": started,
                "result": probe(token, ledger), "finished_at": time.time()}
    from .firestore import make_store
    store = make_store(args.scope, token, ledger, role=args.role)
    if args.command == "inspect-state":
        result = inspect_state(store)
    elif args.command == "concurrency":
        from .concurrency import concurrency
        result = concurrency(store, token, ledger)
    elif args.command == "permission-probe":
        from .permission import probe
        result = probe(store, ledger)
    elif args.command == "response-loss":
        from .response_loss import response_loss
        result = response_loss(store)
    else:
        result = smoke(store) if args.command == "smoke" else scan(store)
    return {"state": "passed", "case": args.command, "project": PROJECT, "database": DATABASE,
            "scope": args.scope, "started_at": started, "finished_at": time.time(),
            "result": result, "limitations": "合成Firestoreのみ。請求読取り回数は未確定"}


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False))
    except Exception as exc:
        # SDK例外にrequest/tokenが含まれていても表示しない。
        print(json.dumps({"state": "failed", "error_type": type(exc).__name__,
                          "error_code": exc.code if isinstance(exc, Refused) else "unexpected_error",
                          "partial_result": False}), file=sys.stderr)
        sys.exit(1)
