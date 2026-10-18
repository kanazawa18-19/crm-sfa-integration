"""停止したMac側の確定計画をクラウドへ引き継ぐ。既定は読取のみ。"""
import argparse
import fcntl
import json
from pathlib import Path

from scripts.backfill_zoho_missed_records import load_env
from src.contact_name_job.domain import validate_plan
from src.contact_name_job.repository import Repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status','import','activate','pause'])
    parser.add_argument('--root', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_env(False)
    repo = Repository()
    if args.action == 'status':
        print(json.dumps(repo.status(), default=str)); return
    if args.action in ('import','activate'):
        if not args.root:
            parser.error('--rootでMac側の実行記録を指定してください')
        with (args.root/'parallel.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            entries = json.loads((args.root/'name-plan.json').read_text())
            if len(entries) != 33042:
                raise ValueError('承認済み33,042件の計画だけを扱います')
            digest = validate_plan(entries)
            verified = set()
            files = [args.root/'name-results.jsonl', args.root/'single-connection2/name-results.jsonl',
                     *[args.root/f'parallel-{i}/name-results.jsonl' for i in range(2,6)]]
            for file in files:
                lines = file.read_text().splitlines(keepends=True)
                for index,line in enumerate(lines):
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        if index == len(lines)-1 and not line.endswith('\n'):
                            print(json.dumps({'incomplete_tail':str(file.relative_to(args.root))}))
                            continue
                        raise
                    if row['state'] == 'verified': verified.add(row['id'])
            if not verified <= {e['id'] for e in entries}:
                raise ValueError('計画外の結果が含まれています')
            print(json.dumps({'total':len(entries),'verified':len(verified),'remaining':len(entries)-len(verified),'plan_hash':digest,'apply':args.apply}))
            if args.apply:
                if args.action == 'import': repo.import_plan(entries, verified)
                else:
                    # インポート済み計画との一致を検証し、進捗は変更しない。
                    repo.import_plan(entries, verified)
                    repo.set_enabled(True)
    elif args.action == 'pause' and args.apply:
        repo.set_enabled(False)


if __name__ == '__main__':
    main()
