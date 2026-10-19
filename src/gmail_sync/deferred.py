"""統合中のメールを本文付きで保全し、他の連絡先を止めずに再開する。"""
from dataclasses import asdict, replace
import time

from psycopg.types.json import Jsonb
from src.gmail_sync import db, gmail_client
from src.sync_engine.record_sync_lock import RecordSyncBusy


class DeferredContactBusy(RecordSyncBusy):
    def __init__(self, page_id, contact_email):
        super().__init__('連絡先の統合完了後にメール保存を再開します')
        self.page_id = page_id
        self.contact_email = contact_email


class _Deferred:
    def __bool__(self):
        return False


DEFERRED = _Deferred()


def enqueue(message, rep_email, page_id, contact_email, internal_domains):
    """最初の本文・照合結果を保持する。保存できなければ呼出元へ例外を返す。"""
    if not page_id or not contact_email:
        raise ValueError('保留対象の連絡先がありません')
    if message.raw_message is None:
        from src.gmail_sync.token_crypto import decrypt_token
        from src.infrastructure.http_budget import http_budget
        connection = db.find_connection_by_email(rep_email)
        if connection is None:
            raise RuntimeError('保留メールの本文を取得できません')
        try:
            with http_budget(15), gmail_client.bounded_rate_limit_retries(0):
                token = gmail_client.refresh_access_token(decrypt_token(connection.refresh_token_enc))
                raw = gmail_client.get_raw_message(token, message.id)
        except Exception:
            # 本文取得時の404も、通常の削除済みメールとして握りつぶさない。
            raise RuntimeError('保留メールの本文を取得できません') from None
        message = replace(message, raw_message=raw)
    with db._connect() as conn:
        conn.execute("SET LOCAL statement_timeout = '5000ms'")
        conn.execute('''INSERT INTO "GmailDeferredMessage"
            ("repEmail", "gmailMessageId", "contactPageId", "contactEmail", message, "internalDomains")
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT ("repEmail","gmailMessageId") DO NOTHING''',
            (rep_email.lower(), message.id, page_id, contact_email.lower(),
             Jsonb(asdict(message)), Jsonb(sorted(internal_domains))))


def _due(limit):
    with db._connect() as conn:
        conn.execute("SET LOCAL statement_timeout = '5000ms'")
        return conn.execute('''SELECT * FROM (
            SELECT *, row_number() OVER (PARTITION BY "contactPageId"
                ORDER BY "nextAttemptAt", "createdAt", "repEmail", "gmailMessageId") AS position
            FROM "GmailDeferredMessage" WHERE state='pending' AND "nextAttemptAt"<=now()
            ) q WHERE position=1 ORDER BY "nextAttemptAt", "createdAt" LIMIT %s''', (limit,)).fetchall()


def _finish(row, error=None):
    with db._connect() as conn:
        conn.execute("SET LOCAL statement_timeout = '5000ms'")
        if error:
            conn.execute('''UPDATE "GmailDeferredMessage" SET attempts=attempts+1,
                "lastErrorType"=%s, "updatedAt"=now(),
                "nextAttemptAt"=now()+make_interval(secs=>least(1800,300*(attempts+1)))
                WHERE "repEmail"=%s AND "gmailMessageId"=%s AND state='pending' ''',
                (type(error).__name__, row['repEmail'], row['gmailMessageId']))
        else:
            # 本文抽出不可の原文は監査・将来回収用に残す。完了後の再配送はしない。
            conn.execute('''UPDATE "GmailDeferredMessage" SET state='done',
                message=CASE WHEN btrim(coalesce(message->>'body_text',''))<>'' THEN '{}'::jsonb ELSE message END,
                attempts=attempts+1,"lastErrorType"=NULL,"updatedAt"=now(),"completedAt"=now()
                WHERE "repEmail"=%s AND "gmailMessageId"=%s AND state='pending' ''',
                (row['repEmail'], row['gmailMessageId']))


def _process(row, client):
    from src.gmail_sync import sync
    message = gmail_client.GmailMessage(**{**row['message'], 'raw_message': None})
    sync.record_message(message, row['repEmail'], client,
        internal_domains=frozenset(row['internalDomains']), atomic_insert=True,
        resolve_contact=lambda address: row['contactPageId']
        if address.lower() == row['contactEmail'] else None, defer_busy=False)
    effect = db.gmail_ingest_effect_status(message.id)
    if not db.email_log_exists(message.id) or (effect is not None and not all(effect.values())):
        raise RuntimeError('保留メールの保存完了を確認できません')


def drain(*, limit=10, seconds=45, client=None):
    """1連絡先1件ずつ。統合待ちは期限切れで捨てず、再試行を後ろへ回す。"""
    from src.infrastructure.http_budget import http_budget
    from src.db_schema.registry import get_schema
    from src.sync_engine.clients.notion_client import HttpNotionClient
    deadline = time.monotonic() + seconds
    lock = db.try_acquire_push_sync_lock('gmail-deferred-worker')
    if lock is None:
        return dict(status='busy')
    counts = dict(status='ok', done=0, waiting=0, failed=0)
    try:
        rows = _due(limit)
        if rows and client is None:
            client = HttpNotionClient('contact', get_schema('contact').notion_database_id)
        for row in rows:
            remaining = deadline - time.monotonic()
            if remaining < 2:
                break
            try:
                with http_budget(min(10, remaining - 1)), gmail_client.bounded_rate_limit_retries(0):
                    _process(row, client)
            except Exception as error:
                _finish(row, error)
                counts['waiting' if isinstance(error, RecordSyncBusy) else 'failed'] += 1
            else:
                _finish(row)
                counts['done'] += 1
        return counts
    finally:
        db.release_push_sync_lock(lock, 'gmail-deferred-worker')


def health():
    with db._connect() as conn:
        conn.read_only = True
        conn.execute("SET LOCAL statement_timeout = '5000ms'")
        row = conn.execute('''SELECT count(*) AS pending,
            count(*) FILTER (WHERE "createdAt"<now()-interval '30 minutes') AS overdue,
            min("createdAt") AS oldest, max("updatedAt") AS last_attempt
            FROM "GmailDeferredMessage" WHERE state='pending' ''').fetchone()
    return dict(deferred_pending_count=row['pending'], deferred_overdue_count=row['overdue'],
                deferred_oldest_at=row['oldest'].isoformat() if row['oldest'] else None,
                deferred_last_attempt_at=row['last_attempt'].isoformat() if row['last_attempt'] else None)
