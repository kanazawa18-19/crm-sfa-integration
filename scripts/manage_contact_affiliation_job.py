#!/usr/bin/env python3
"""定期所属照合の状態確認・無効化・段階的な有効化。"""
import argparse
import json
from src.contact_affiliation_job.repository import Repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'pause', 'dry-run', 'enable', 'import-evidence', 'discover', 'candidates', 'history'])
    parser.add_argument('--file', help='原本由来を明示したJSON配列')
    parser.add_argument('--output', help='顧客情報を含む私有JSONの新規保存先')
    parser.add_argument('--contact-id')
    parser.add_argument('--batches', type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.batches <= 1000:
        parser.error('--batchesは1〜1000です')
    repository = Repository()
    if args.command in ('candidates', 'history'):
        import os
        from pathlib import Path
        from uuid import UUID
        if not args.output:
            parser.error('--outputに私有保存先を指定してください')
        path = Path(args.output).expanduser().resolve()
        if path.is_relative_to(Path(__file__).resolve().parents[1]) or path.is_relative_to(Path.home() / 'notes'):
            parser.error('出力先はリポジトリ・Vaultの外を指定してください')
        key = str(UUID(args.contact_id)) if args.contact_id else None
        rows = repository.report(args.command, key)
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            json.dump(rows, stream, ensure_ascii=False, default=str, indent=2)
        print(json.dumps({'exported': len(rows), 'limit': 1000}, ensure_ascii=False))
    elif args.command == 'import-evidence':
        from pathlib import Path
        if not args.file:
            parser.error('--fileが必要です')
        repository.import_evidence(json.loads(Path(args.file).read_text()))
    elif args.command == 'discover':
        from src.contact_affiliation_job.worker import run
        for _ in range(args.batches):
            result = run(discover_only=True)
            print(json.dumps(result, ensure_ascii=False))
            if result.get('status') != 'ok' or result.get('discovery_complete'):
                break
    elif args.command != 'status':
        repository.configure(enabled=args.command != 'pause', dry_run=args.command != 'enable')
    print(json.dumps(repository.status(), ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
