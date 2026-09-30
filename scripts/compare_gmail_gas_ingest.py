"""GAS実行ログとEmailLogの同一期間・同一担当者のID集合を双方向照合する。読み取り専用。"""
from __future__ import annotations

import argparse
import json
import os
import statistics
from datetime import datetime, timezone
from pathlib import Path

import psycopg


def _utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('日時にはタイムゾーンが必要です')
    return parsed.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gas_jsonl', type=Path)
    parser.add_argument('--rep-email', required=True)
    parser.add_argument('--start', required=True, help='ISO8601、開始を含む')
    parser.add_argument('--end', required=True, help='ISO8601、終了を含まない')
    args = parser.parse_args()
    start, end = _utc(args.start), _utc(args.end)
    if start >= end:
        parser.error('終了は開始より後にしてください')
    gas_ids: set[str] = set()
    unmatched_ids: set[str] = set()
    failed_ids: set[str] = set()
    intervals: list[tuple[datetime, datetime]] = []
    observed_at: list[datetime] = []
    first_latency_by_id: dict[str, float] = {}
    statuses = {'would_insert', 'existing', 'inserted', 'postprocess_resumed'}
    for line in args.gas_jsonl.read_text().splitlines():
        entry = json.loads(line)
        if not isinstance(entry.get('results'), list):
            continue
        if 'after' in entry and 'before' in entry:
            intervals.append((
                datetime.fromtimestamp(int(entry['after']), timezone.utc),
                datetime.fromtimestamp(int(entry['before']), timezone.utc),
            ))
        observed = _utc(entry['observedAt']) if entry.get('observedAt') else None
        if observed:
            observed_at.append(observed)
        for item in entry['results']:
            if not item.get('internal_date_ms'):
                continue
            sent_at = datetime.fromtimestamp(int(item['internal_date_ms']) / 1000, timezone.utc)
            if start <= sent_at < end:
                if item.get('status') == 'unmatched':
                    unmatched_ids.add(item['id'])
                    continue
                if item.get('status') == 'effect_failed':
                    failed_ids.add(item['id'])
                    continue
                if item.get('status') not in statuses:
                    continue
                gas_ids.add(item['id'])
                if observed:
                    delay = max(0.0, (observed - sent_at).total_seconds())
                    first_latency_by_id[item['id']] = min(first_latency_by_id.get(item['id'], delay), delay)
    ordered_observations = sorted(set(observed_at))
    gaps = [
        {'from': a.isoformat(), 'to': b.isoformat(), 'minutes': round((b - a).total_seconds() / 60, 1)}
        for a, b in zip(ordered_observations, ordered_observations[1:])
        if (b - a).total_seconds() > 30 * 60
    ]
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DATABASE_URL が必要です')
    covered_until = start
    for interval_start, interval_end in sorted(intervals):
        if interval_start > covered_until:
            continue
        covered_until = max(covered_until, interval_end)
    full_scan_coverage = covered_until >= end
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT "gmailMessageId" FROM "EmailLog" '
            'WHERE lower("repEmail") = lower(%s) AND "sentAt" >= %s AND "sentAt" < %s',
            (args.rep_email, start, end),
        )
        crm_ids = {row[0] for row in cur.fetchall()}
        cur.execute(
            'SELECT "gmailMessageId" FROM "EmailLog" '
            'WHERE lower("repEmail") = lower(%s) AND "gmailMessageId" = ANY(%s)',
            (args.rep_email, list(gas_ids)),
        )
        gas_existing_any_time = {row[0] for row in cur.fetchall()}
    print(json.dumps({
        'start': start.isoformat(), 'end': end.isoformat(),
        'gas_first_observed': min(observed_at).isoformat() if observed_at else None,
        'gas_last_observed': max(observed_at).isoformat() if observed_at else None,
        'observation_gaps_over_30_min': gaps,
        'first_observed_delay_median_seconds': statistics.median(first_latency_by_id.values()) if first_latency_by_id else None,
        'gas_matched_count': len(gas_ids), 'crm_count': len(crm_ids),
        'full_scan_coverage': full_scan_coverage,
        'unmatched_gas_ids': sorted(unmatched_ids),
        'effect_failed_ids': sorted(failed_ids),
        'gas_missing_in_crm': sorted(gas_ids - gas_existing_any_time),
        'gas_time_mismatch': sorted(gas_existing_any_time - crm_ids),
        'crm_not_seen_by_gas': sorted(crm_ids - gas_ids - unmatched_ids),
    }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
