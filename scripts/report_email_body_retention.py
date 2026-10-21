"""本文保存期間の比較用集計。削除・更新・本文出力は一切行わない。"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os

import psycopg
from psycopg.rows import dict_row


def report(conn, *, days: int, now: datetime) -> dict:
    if not 1 <= days <= 36500 or now.tzinfo is None:
        raise ValueError('日数は1〜36500、基準時刻はタイムゾーン付きで指定してください')
    cutoff = now - timedelta(days=days)
    # 更新時刻で試算する。受信日時や本文の保存開始日時とは同じではない。
    rows = conn.execute('''SELECT COALESCE(data->>'state','unknown') AS state,
        ("updatedAt" < %s) AS older_than_cutoff, count(*) AS records,
        sum(octet_length(COALESCE(data#>>'{message,body_text}',''))) AS body_bytes
        FROM "EmailReview" GROUP BY 1,2 ORDER BY 1,2''', (cutoff,)).fetchall()
    return {'mode': 'report_only', 'retention_days_candidate': days,
            'observed_at': now.isoformat(), 'cutoff': cutoff.isoformat(),
            'age_basis': 'review_updated_at', 'deletion_enabled': False,
            'groups': [dict(row) for row in rows]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, required=True, help='試算する日数。運用設定は変更しない')
    parser.add_argument('--database-env', default='DATABASE_URL_UNPOOLED')
    args = parser.parse_args()
    if not 1 <= args.days <= 36500:
        parser.error('--days は1〜36500')
    try:
        with psycopg.connect(os.environ[args.database_env], row_factory=dict_row,
                             connect_timeout=10, options='-c default_transaction_read_only=on -c statement_timeout=30000') as conn:
            result = report(conn, days=args.days, now=datetime.now(timezone.utc))
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    except Exception as exc:
        # 接続文字列やメール本文を例外メッセージ経由で表示しない。
        raise SystemExit('保存期間の試算を中断しました: ' + type(exc).__name__) from None


if __name__ == '__main__':
    main()
