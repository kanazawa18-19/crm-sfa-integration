"""私有の事前計画に従いNotionの姓・名の空欄を補完する。既定はdry-run。"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from scripts.backfill_zoho_missed_records import load_env
from src.email_review.contact_completion import name_parts
from src.email_review.gateway import Gateway, record_lock
from src.audit_log.actor_context import set_actor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    load_env(args.apply)
    logging.disable(logging.CRITICAL)
    entries = json.loads(args.plan.read_text())
    gateway = Gateway()
    log_path = args.plan.with_name('name-results.jsonl')
    done = set()
    if log_path.exists():
        done = {r['id'] for line in log_path.read_text().splitlines() if (r := json.loads(line))['state'] == 'verified'}
    counts = {'planned': len(entries), 'already_verified': len(done), 'verified': 0, 'held': 0, 'errors': 0}
    for entry in entries:
        if entry['id'] in done:
            continue
        if not args.apply:
            continue
        result = {'id': entry['id']}
        try:
            with set_actor('manual', '2026-10-17本人指示：既存連絡先の姓・名空欄補完'):
                with record_lock('contact', entry['id']):
                    current = gateway.live('contact', entry['id'])
                    keys = ('名前', '姓', '名')
                    # 既存の入力変更と競合したら上書きしない。再開時の保存済み結果は確認だけ。
                    expected = {**entry['before'], **entry['changes']}
                    if all((current.get(k) or '') == (expected.get(k) or '') for k in keys):
                        result['state'] = 'verified'
                    elif any((current.get(k) or '') != (entry['before'].get(k) or '') for k in keys):
                        result['state'] = 'held'
                    else:
                        changes = name_parts(current)
                        if changes != entry['changes']:
                            result['state'] = 'held'
                        else:
                            gateway.client('contact').update_page(entry['id'], changes)
                            saved = gateway.live('contact', entry['id'])
                            if any(saved.get(k) != v for k, v in changes.items()):
                                raise ValueError('保存値不一致')
                            result['state'] = 'verified'
            counts['verified' if result['state'] == 'verified' else 'held'] += 1
        except Exception as exc:
            result.update(state='error', error_type=type(exc).__name__)
            counts['errors'] += 1
        with log_path.open('a') as stream:
            stream.write(json.dumps(result, ensure_ascii=False) + '\n')
        args.plan.with_name('name-progress.json').write_text(json.dumps(counts))
        if (counts['verified'] + counts['held'] + counts['errors']) % 25 == 0:
            print(json.dumps(counts), flush=True)
    print(json.dumps({**counts, 'apply': args.apply, 'finished': True}), flush=True)
    return int(bool(counts['errors']))


if __name__ == '__main__':
    raise SystemExit(main())
