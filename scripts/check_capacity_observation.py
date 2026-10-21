"""保存済み観測とworker成功時刻から通知用JSONを作る。通信・自動回復なし。"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.sync_capacity.health import assess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('observation', type=Path)
    parser.add_argument('--last-worker-success', type=float, help='成功または正常idleの実測epoch秒。未取得なら省略')
    parser.add_argument('--freshness-seconds', type=float, required=True)
    parser.add_argument('--waiting-seconds', type=float, required=True)
    parser.add_argument('--processing-seconds', type=float, required=True)
    args = parser.parse_args()
    try:
        result = assess(json.loads(args.observation.read_text()), now=time.time(),
                        last_worker_success=args.last_worker_success,
                        freshness_seconds=args.freshness_seconds,
                        waiting_seconds=args.waiting_seconds, processing_seconds=args.processing_seconds)
    except (OSError, ValueError):
        result = {'healthy': False, 'alerts': ['observation_unreadable'], 'automatic_recovery': False}
    print(json.dumps(result))
    return 0 if result['healthy'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
