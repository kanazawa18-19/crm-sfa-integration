"""GAS観測の開始・ID別判定・終端を、メール本文なしで永続化する。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from psycopg.types.json import Jsonb
from src.gmail_sync import db


def validate(value, *, dry_run, messages):
    if value is None:
        return None  # 旧GASとの互換。永続観測の合格証拠にはしない。
    if not isinstance(value, dict):
        raise ValueError('observationが不正です')
    scan_id = value.get('scan_id')
    if not isinstance(scan_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', scan_id):
        raise ValueError('scan_idが不正です')
    phase = value.get('phase')
    kind = value.get('kind')
    if phase not in ('started', 'batch', 'retry', 'complete', 'missing') or kind not in ('natural', 'backfill'):
        raise ValueError('観測の種類が不正です')
    if kind == 'backfill' and not dry_run:
        raise ValueError('補完走査はdry-run専用です')
    after, before = value.get('after'), value.get('before')
    if (type(after) is not int or type(before) is not int or
            not 0 <= after < before <= int(datetime.now(timezone.utc).timestamp()) + 300 or
            before - after > 31 * 86400):
        raise ValueError('観測範囲が不正です')
    seq = value.get('sequence', 0)
    if type(seq) is not int or not 0 <= seq <= 100000:
        raise ValueError('観測連番が不正です')
    if phase in ('started', 'complete', 'missing') and messages:
        raise ValueError('開始と終端にはメールを含めません')
    if phase == 'started' and seq != 0:
        raise ValueError('開始連番が不正です')
    total = value.get('listed', 0)
    if type(total) is not int or not 0 <= total <= 1000000:
        raise ValueError('観測件数が不正です')
    missing_ids = value.get('missing_ids', [])
    if phase == 'missing':
        if (not isinstance(missing_ids, list) or len(missing_ids) != 1
                or not isinstance(missing_ids[0], str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', missing_ids[0])):
            raise ValueError('消失IDが不正です')
    elif missing_ids:
        raise ValueError('消失IDのフェーズが不正です')
    return dict(missing_ids=missing_ids, scan_id=scan_id, phase=phase, kind=kind, after=after, before=before,
                sequence=seq, listed=total)


def record(meta, *, rep_email, dry_run, messages, process):
    """同じ走査をDB行で直列化し、応答紛失時は保存済みの応答を返す。"""
    received_at = datetime.now(timezone.utc)
    scan = meta['scan_id']
    phase = meta['phase']
    event_key = meta['missing_ids'][0] if phase == 'missing' else meta['sequence']
    event_id = f"{scan}:{phase}:{event_key}"
    signature = dict(meta, rep_email=rep_email, dry_run=dry_run,
                     messages=[{'id': m.id, 'internal_date_ms': m.internal_date_ms} for m in messages])
    digest = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
    base = dict(dryRun=dry_run, observationKind=meta['kind'], scanId=scan,
                after=meta['after'], before=meta['before'])
    with db._connect() as conn:
        if phase == 'started':
            conn.execute(
                'INSERT INTO "GmailIngestObservation" ("eventId","scanId",phase,"requestHash",evidence,response,"receivedAt") '
                'VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT ("eventId") DO NOTHING',
                (event_id, scan, phase, digest, Jsonb(dict(base, repEmail=rep_email)),
                 Jsonb({'received': 0, 'dry_run': dry_run, 'results': [], 'observation_saved': True}), received_at),
            )
        start = conn.execute(
            'SELECT evidence FROM "GmailIngestObservation" WHERE "eventId"=%s FOR UPDATE',
            (f'{scan}:started:0',),
        ).fetchone()
        if not start or start['evidence'] != dict(base, repEmail=rep_email):
            raise ValueError('開始記録がないか走査条件が競合しています')
        previous = conn.execute(
            'SELECT "requestHash",response FROM "GmailIngestObservation" WHERE "eventId"=%s',
            (event_id,),
        ).fetchone()
        if previous:
            if previous['requestHash'] != digest:
                raise ValueError('同じ観測IDの内容が競合しています')
            return previous['response']
        if conn.execute('SELECT 1 FROM "GmailIngestObservation" WHERE "scanId"=%s AND phase=\'complete\'',
                        (scan,)).fetchone():
            raise ValueError('完了済み走査に追加できません')
        if phase == 'complete':
            if conn.execute('SELECT 1 FROM "GmailIngestObservation" WHERE "scanId"=%s AND phase=\'missing\'', (scan,)).fetchone():
                raise ValueError('消失を含む走査は完了にできません')
            batches = conn.execute(
                'SELECT evidence FROM "GmailIngestObservation" WHERE "scanId"=%s AND phase=\'batch\'',
                (scan,),
            ).fetchall()
            seqs = sorted(row['evidence']['sequence'] for row in batches)
            count = sum(len(row['evidence']['results']) for row in batches)
            ids = [item['id'] for row in batches for item in row['evidence']['results']]
            if seqs != list(range(meta['sequence'])) or count != meta['listed']:
                raise ValueError('全バッチの保存件数が終端と一致しません')
        response = process() if phase in ('batch', 'retry') else {
            'received': 0, 'dry_run': dry_run, 'results': [],
        }
        response['observation_saved'] = True
        evidence = dict(base, sequence=meta['sequence'], decidedAt=datetime.now(timezone.utc).isoformat())
        if phase in ('batch', 'retry'):
            evidence['results'] = [dict(item, internal_date_ms=message.internal_date_ms)
                                   for item, message in zip(response['results'], messages)]
            evidence['retry'] = phase == 'retry'
        elif phase == 'complete':
            from collections import Counter
            duplicates = {key: count for key, count in Counter(ids).items() if count > 1}
            evidence.update(scanComplete=True, listed=meta['listed'], uniqueListed=len(set(ids)),
                            duplicateOccurrences=duplicates)
        elif phase == 'missing':
            evidence.update(missingIds=meta['missing_ids'], scanIncomplete=True)
        conn.execute(
            'INSERT INTO "GmailIngestObservation" ("eventId","scanId",phase,"requestHash",evidence,response,"receivedAt") '
            'VALUES (%s,%s,%s,%s,%s,%s,%s)',
            (event_id, scan, phase, digest, Jsonb(evidence), Jsonb(response), received_at),
        )
        return response
