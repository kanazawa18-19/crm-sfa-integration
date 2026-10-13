"""作成前保留と問い合わせ保存後の引継ぎを、通常配送の未完了と別に観測する。"""
import hashlib
import json
from src.diagnostics.webhook_health import check
from src.hub_creation.journal import connect


def read_registration_health():
    with connect() as conn:
        conn.read_only = True
        conn.execute("SET LOCAL statement_timeout='5000ms'")
        pending = conn.execute('''SELECT 'creation' AS kind,"sourceKey",target,state,"createdAt" AS since
            FROM "HubCreationAttempt" WHERE state<>'created'
            UNION ALL SELECT 'handoff',"sourceKey",'',state,"createdAt"
            FROM "InquiryHandoff" WHERE state<>'done'
            ORDER BY since,"sourceKey",target''').fetchall()
        rows = conn.execute('''SELECT state,count(*) AS count FROM "HubCreationAttempt"
            WHERE state<>'created' GROUP BY state''').fetchall()
        intake = conn.execute('''SELECT state,count(*) AS count FROM "InquiryHandoff"
            WHERE state<>'done' GROUP BY state''').fetchall()
    counts = {row['state']: row['count'] for row in rows}
    intake_counts = {row['state']: row['count'] for row in intake}
    status = 'unknown' if counts.get('reserved') else 'pending' if counts or intake_counts else 'ok'
    reason = ('作成結果が未確定の予約があります。自動再作成せず確認してください' if status == 'unknown'
              else '登録・問い合わせ引継ぎの保留があります' if status == 'pending'
              else '登録・問い合わせ引継ぎの保留はありません')
    oldest = pending[0] if pending else None
    fingerprint = hashlib.sha256(json.dumps([oldest[k] for k in ('kind', 'sourceKey', 'target')]).encode()).hexdigest() if oldest else None
    return check('registration_holds', status, reason, creation_attempts=counts,
                 inquiry_handoffs=intake_counts, count=len(pending),
                 activity_at=oldest['since'].isoformat() if oldest else None, fingerprint=fingerprint, count_unit='attempts_and_handoffs_not_unique_records')
