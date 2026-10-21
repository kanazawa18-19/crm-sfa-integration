"""不足ID一覧を作成台帳と照合する。再POST・対応表更新・予約解除は行わない。"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


def classify_gaps(inventory, attempts, *, now):
    observed = datetime.fromisoformat(inventory['observed_at'])
    started = datetime.fromisoformat(inventory['scan_started_at'])
    if (inventory.get('resumed') is not False or observed.tzinfo is None or started.tzinfo is None
            or not 0 <= (now - started).total_seconds() <= 21600 or not started <= observed <= now):
        raise ValueError('6時間以内に完走した一覧が必要です')
    sources_by_page, attempts_by_target = defaultdict(set), defaultdict(list)
    for row in attempts:
        if row['dbKey'] != inventory['db']:
            continue
        if row['target'] == 'notion' and row['state'] == 'created':
            sources_by_page[row.get('externalId')].add(row['sourceKey'])
        attempts_by_target[(row['sourceKey'], row['target'])].append(row)
    results = []
    for gap in inventory['missing_targets']:
        page_id, target = gap['id'], gap['target']
        reason = gap['classification']
        if reason == 'live_verification_required':
            sources = sources_by_page[page_id]
            if len(sources) > 1:
                reason = 'ambiguous_creation_source'
            else:
                source = next(iter(sources), 'notion:' + page_id)
                rows = attempts_by_target[(source, target)]
                if len(rows) > 1:
                    reason = 'ambiguous_creation_attempt'
                elif not rows:
                    reason = 'registration_intent_unconfirmed'
                else:
                    reason = {'reserved': 'creation_result_unknown',
                              'created': 'mapping_restore_requires_readback',
                              'blocked': 'input_or_identity_review_required'}.get(rows[0]['state'], 'unknown_attempt_state')
        results.append({**gap, 'classification': reason, 'automatic_recovery': False})
    return {'db': inventory['db'], 'observed_at': now.isoformat(), 'inventory_observed_at': inventory['observed_at'],
            'inventory_scan_started_at': inventory['scan_started_at'],
            'counts': dict(Counter(row['classification'] for row in results)), 'missing_targets': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inventory', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--database-env', default='DATABASE_URL_UNPOOLED')
    args = parser.parse_args()
    try:
        inventory = json.loads(args.inventory.read_text())
        now = datetime.now(timezone.utc)
        classify_gaps(inventory, [], now=now)  # 古い一覧ならDBへ接続しない。
        with psycopg.connect(os.environ[args.database_env], row_factory=dict_row, connect_timeout=10,
                             options='-c default_transaction_read_only=on -c statement_timeout=30000') as conn:
            attempts = conn.execute('''SELECT "sourceKey", target, "dbKey", state, "externalId"
                FROM "HubCreationAttempt" WHERE "dbKey"=%s''', (inventory['db'],)).fetchall()
        result = classify_gaps(inventory, attempts, now=datetime.now(timezone.utc))
        # 既存計画は上書きせず、作成時から本人だけに読めるようにする。
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
        print(json.dumps(result['counts'], ensure_ascii=False))
    except Exception as exc:
        raise SystemExit('不足先の分類を中断しました: ' + type(exc).__name__) from None


if __name__ == '__main__':
    main()
