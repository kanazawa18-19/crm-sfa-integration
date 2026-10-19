"""旧取込の不足先を、固定した原本・送り先だけで復旧する。既定は計画保存。"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.backfill_zoho_missed_records import load_env
from src.db_schema.base import Tool
from src.record_merge.domain import digest
from src.sync_engine.production_wiring import get_production_wiring
from src.sync_engine.sync_event import SyncEvent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('plan', type=Path)
    parser.add_argument('--db-key')
    parser.add_argument('--id')
    parser.add_argument('--target', choices=['zoho', 'kintone'])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_env(False)
    service = get_production_wiring().creation_service
    if service is None:
        raise ValueError('新規登録の設定が無効です')
    if not args.apply:
        if not all((args.db_key, args.id, args.target)):
            parser.error('計画には --db-key / --id / --target が必要です')
        raw = service.notion_clients[args.db_key].get_raw_page(args.id)
        plan = {'db_key': args.db_key, 'id': args.id, 'target': args.target,
                'hash': digest(service._creation_properties(args.db_key, raw))}
        with args.plan.open('x') as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        args.plan.chmod(0o600)
        print(json.dumps(plan, ensure_ascii=False))
        return
    plan = json.loads(args.plan.read_text())
    event = SyncEvent(Tool.NOTION, plan['db_key'], plan['id'], datetime.now(timezone.utc))
    result = service.handle(event, recovery=plan)
    source = service.journal.source_for_page(plan['id']) or 'notion:' + plan['id']
    mapping = service.store.get(plan['id'])
    external_id = getattr(mapping, {'zoho': 'zoho_id', 'kintone': 'kintone_id'}[plan['target']]) if mapping else None
    print(json.dumps({'result': result, 'external_id': external_id, 'attempt': service.journal.get(source, plan['target'])},
                     ensure_ascii=False, default=str))
    if not external_id:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
