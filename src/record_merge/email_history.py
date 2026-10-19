"""連絡先統合時のメール参照移管。本文・日時・通知履歴は変更しない。"""
import json
from datetime import datetime, timezone

from src.record_merge.domain import MergeHeld, digest
from src.sync_operations.product_holds import connect


def latest_date(*values):
    dates = []
    for value in values:
        if value in (None, ''):
            continue
        try:
            date = datetime.fromisoformat(value.replace('Z', '+00:00'))
            dates.append(date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date)
        except (ValueError, AttributeError, TypeError):
            raise MergeHeld('最終メール日時を確認できません') from None
    return max(dates).astimezone(timezone.utc).isoformat() if dates else None


def _evidence(row):
    # 配信済み印は別処理が更新する。移管では触らず、メール本体だけを照合する。
    immutable = {k: v for k, v in row.items() if k not in {'contactPageId', 'digestedAt'}}
    immutable = json.loads(json.dumps(immutable, default=str))
    return {'id': row['id'], 'sentAt': latest_date(str(row['sentAt'])), 'hash': digest(immutable)}


def notion_date(*values):
    """Notionの実保存精度へ揃える。メール本体の秒は変更しない。"""
    latest = latest_date(*values)
    return datetime.fromisoformat(latest).replace(second=0, microsecond=0).isoformat() if latest else None


def snapshot(source, target):
    with connect() as conn:
        rows = conn.execute('SELECT * FROM "EmailLog" WHERE "contactPageId" = ANY(%s) ORDER BY id',
                            ([source, target],)).fetchall()
    return {'rows': [_evidence(row) for row in rows if row['contactPageId'] == source],
            'latest': latest_date(*(str(row['sentAt']) for row in rows))}


def make_step(source, target, evidence):
    rows = evidence['rows']
    return {'kind': 'email_history', 'dbKey': 'contact', 'id': source, 'targetId': target,
            'rows': rows,
            'before': [{'id': r['id'], 'contactPageId': source} for r in rows],
            'desired': [{'id': r['id'], 'contactPageId': target} for r in rows]}


def _read(conn, step, *, lock=False):
    ids = [row['id'] for row in step['rows']]
    rows = conn.execute('SELECT * FROM "EmailLog" WHERE "contactPageId"=%s OR id=ANY(%s) ORDER BY id'
                        + (' FOR UPDATE' if lock else ''), (step['id'], ids)).fetchall()
    if [_evidence(row) for row in rows] != step['rows']:
        raise MergeHeld('比較後に統合元のメールが追加・変更・削除されました')
    current = [{'id': row['id'], 'contactPageId': row['contactPageId']} for row in rows]
    if current not in (step['before'], step['desired']):
        raise MergeHeld('メールの参照先が第三者に変更されています')
    return current


def read(step):
    with connect() as conn:
        return _read(conn, step)


def write(step):
    with connect() as conn:
        current = _read(conn, step, lock=True)
        if current == step['desired']:
            return
        ids = [row['id'] for row in step['rows']]
        updated = conn.execute('UPDATE "EmailLog" SET "contactPageId"=%s '
                               'WHERE "contactPageId"=%s AND id=ANY(%s) RETURNING id',
                               (step['targetId'], step['id'], ids)).fetchall()
        if len(updated) != len(ids) or _read(conn, step) != step['desired']:
            raise MergeHeld('メール参照の移管結果を確認できません')
