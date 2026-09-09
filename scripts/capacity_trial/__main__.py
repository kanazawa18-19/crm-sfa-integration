"""明示した小規模ケースだけ実行し、終了時はJSON証跡を返す。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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


def main():
    parser = argparse.ArgumentParser(description="承認済み専用Firestore・Neonの隔離試験。本番同期は対象外")
    parser.add_argument("command", choices=["init-ledger", "migrate-target", "record-cost", "smoke", "scan", "neon-probe"])
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--created-at", type=float)
    parser.add_argument("--usd", type=float)
    parser.add_argument("--observed-at", type=float)
    parser.add_argument("--evidence", default="")
    parser.add_argument("--cost-basis", choices=["metered", "free-plan-verified"], default="metered")
    parser.add_argument("--scope", default="trial-smoke")
    parser.add_argument("--role", choices=ACCOUNTS, default="runner")
    args = parser.parse_args()
    if args.command == "init-ledger":
        if args.created_at is None:
            raise Refused("実資源の作成UTC epochが必要")
        Ledger.initialize(args.ledger, args.created_at)
        return {"state": "ledger_initialized", "cost": "未観測・実行禁止"}
    ledger = Ledger(args.ledger)
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
    if args.command == "scan" and args.role != "observer":
        raise Refused("scanは観測用roleのみ")
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
                   "PYTHONNOUSERSITE": "1", "CAPACITY_TRIAL_CHILD": "1"}
            try:
                child = subprocess.run([sys.executable, "-s", "-m", "scripts.capacity_trial", *sys.argv[1:]],
                    cwd=Path(__file__).resolve().parents[2], env=env, input=token.stdout.strip(),
                    capture_output=True, text=True, timeout=60)
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
