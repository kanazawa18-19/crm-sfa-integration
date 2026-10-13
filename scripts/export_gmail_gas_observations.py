"""永続化したGAS観測を比較器用JSONLへ出力する。メール本文は含まない。"""
import argparse
import json
import os
from pathlib import Path

from src.gmail_sync import db


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--rep-email', required=True)
    args = parser.parse_args()
    with db._connect() as conn:
        conn.execute('SET TRANSACTION READ ONLY')
        rows = conn.execute(
            'SELECT o.phase,o.evidence,o."receivedAt" FROM "GmailIngestObservation" o '
            'JOIN "GmailIngestObservation" s ON s."eventId"=o."scanId" || \':started:0\' '
            'WHERE lower(s.evidence->>\'repEmail\')=lower(%s) ORDER BY o."receivedAt",o."eventId"',
            (args.rep_email,),
        ).fetchall()
    # 開始時に600で作り、宛先メールはエクスポートしない。
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        for row in rows:
            evidence = {key: value for key, value in row['evidence'].items() if key != 'repEmail'}
            field = 'completedAt' if row['phase'] == 'complete' else 'observedAt'
            evidence['receivedAt'] = row['receivedAt'].isoformat()
            evidence[field] = evidence.get('decidedAt', evidence['receivedAt'])
            evidence['phase'] = row['phase']
            stream.write(json.dumps(evidence, ensure_ascii=False) + '\n')
    print(f'{len(rows)}件の観測イベントを保存しました')


if __name__ == '__main__':
    main()
