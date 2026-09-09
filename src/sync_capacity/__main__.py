"""運用者用CLI。初期化・復旧は既定dry-run、本文や認証値は表示しない。"""

import argparse
import json
import sys
import time

from src.sync_capacity.firestore_store import enabled, get_store


def main() -> int:
    parser = argparse.ArgumentParser(description="共有実行枠と永続ジョブの操作")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("initialize")
    init.add_argument("--apply", action="store_true")
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--state", choices=["pending", "retry", "processing", "completed", "needs_attention"],
                         default="processing")
    inspect.add_argument("--limit", type=int, default=50)
    recover = sub.add_parser("recover")
    recover.add_argument("--job-id", required=True)
    recover.add_argument("--owner", required=True)
    recover.add_argument("--apply", action="store_true")
    recover.add_argument("--confirm-worker-stopped", action="store_true")
    sub.add_parser("drain")
    args = parser.parse_args()
    try:
        store = get_store()
        if args.command == "initialize":
            result = store.initialize(apply=args.apply)
        elif args.command == "recover":
            result = store.recover(args.job_id, args.owner, now=time.time(), apply=args.apply,
                                   stopped=args.confirm_worker_stopped)
        elif args.command == "inspect":
            from google.cloud.firestore_v1.base_query import FieldFilter
            if not 1 <= args.limit <= 500:
                raise ValueError("limit must be 1..500")
            fields = ("state", "source", "owner", "slot", "attempts", "created_at", "updated_at", "reason")
            result = [{"job_id": snap.id, **{key: snap.to_dict().get(key) for key in fields}}
                      for snap in store.jobs.where(filter=FieldFilter("state", "==", args.state))
                      .limit(args.limit).stream()]
        else:
            if not enabled():
                raise ValueError("queue disabled")
            from src.sync_capacity.worker import run_worker
            result = run_worker()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception:
        # SDK例外本文に認証値・接続情報が含まれる可能性がある。
        print("処理結果を確定できません。設定・共有枠・ジョブ状態を確認してください。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
