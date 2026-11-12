"""実PostgreSQLで観測対象・走査区分・完了時刻の選別を確認する。"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from psycopg.types.json import Jsonb

from src.diagnostics import webhook_health
from src.gmail_sync import deferred
from tests.gmail_sync.test_observation import local  # noqa: F401


def test_対象者の自然走査だけから最新完了を選ぶ(local, monkeypatch):
    now = datetime(2026, 11, 12, 9, tzinfo=timezone.utc)
    monkeypatch.setenv('DATABASE_URL', 'local-test-only')
    monkeypatch.setenv('GMAIL_INGEST_REP_EMAIL', ' OWNER@example.invalid ')
    monkeypatch.setattr(webhook_health, 'psycopg', SimpleNamespace(connect=lambda *a, **k: local()))
    monkeypatch.setattr(deferred, 'health', lambda: {'deferred_overdue_count': 0})

    def scan(key, minutes, *, owner='owner@example.invalid', kind='natural', complete=True, canonical=True):
        with local() as conn:
            for phase in ('started', 'complete') if complete else ('started',):
                event_id = key + (':started:0' if phase == 'started' and canonical else ':' + phase + ':1')
                conn.execute('''INSERT INTO "GmailIngestObservation"
                    ("eventId","scanId",phase,"requestHash",evidence,response,"receivedAt")
                    VALUES (%s,%s,%s,'test',%s,'{}',%s)''',
                    (event_id, key, phase, Jsonb({'repEmail': owner, 'observationKind': kind}),
                     now - timedelta(minutes=minutes)))

    scan('older', 40)
    scan('latest-valid', 5, owner='Owner@Example.Invalid')
    scan('different-owner', 1, owner='other@example.invalid')
    scan('backfill', 1, kind='backfill')
    scan('incomplete', 1, complete=False)
    scan('invalid-start-id', 1, canonical=False)
    result = webhook_health.read_gmail_observation(now)
    assert result['status'] == 'ok'
    assert result['last_received_at'] == (now - timedelta(minutes=5)).isoformat()


def test_対象走査なしは正常としない(local, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'local-test-only')
    monkeypatch.setenv('GMAIL_INGEST_REP_EMAIL', 'owner@example.invalid')
    monkeypatch.setattr(webhook_health, 'psycopg', SimpleNamespace(connect=lambda *a, **k: local()))
    monkeypatch.setattr(deferred, 'health', lambda: {'deferred_overdue_count': 0})
    result = webhook_health.read_gmail_observation(datetime.now(timezone.utc))
    assert result['status'] == 'unknown'
    assert result['last_received_at'] is None
