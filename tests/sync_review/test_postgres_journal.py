"""明示した使い捨てローカルDBだけで台帳の実SQLを検証する。"""
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse
import os
import uuid

import psycopg
from psycopg import sql
import pytest

from src.sync_review.journal import FieldReviewJournal
from src.sync_review.domain import ReviewForbidden, ReviewConflict


@pytest.fixture
def journal(monkeypatch):
    dsn = os.environ.get('SYNC_REVIEW_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('明示した使い捨てローカルDBのみで実施')
    parsed = urlparse(dsn)
    assert parsed.hostname in {'127.0.0.1', 'localhost'}
    assert parsed.path == '/crm_field_review_test'
    schema = 'review_' + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    scoped = dsn + ('&' if '?' in dsn else '?') + 'options=' + quote('-csearch_path=' + schema)
    monkeypatch.setenv('DATABASE_URL', scoped)
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute('CREATE TABLE "User" (id TEXT PRIMARY KEY,"isManager" BOOLEAN NOT NULL)')
            conn.execute('INSERT INTO "User" VALUES (%s,true),(%s,false)', ('manager', 'viewer'))
            conn.execute(Path('dashboard/prisma/migrations/20260928050000_sync_field_review/migration.sql').read_text())
        yield FieldReviewJournal()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def request(journal):
    return journal.enqueue(db_key='client_master', notion_key='page', property_name='取引先名',
        source_tool='notion', event_at=datetime(2026, 9, 28, tzinfo=timezone.utc), snapshot={
            'notion': {'supported': True, 'id': 'page', 'field': '取引先名', 'value': '', 'canonical': ''},
            'zoho': {'supported': True, 'id': 'zoho', 'field': 'Account_Name', 'value': '元値', 'canonical': '元値'},
        })


def test_real_migration_idempotent_enqueue_two_stage_and_history(journal):
    row = request(journal)
    assert request(journal)['id'] == row['id']
    with pytest.raises(ReviewForbidden):
        journal.decision(row['id'], actor_id='viewer', action='confirm', revision=0)
    with pytest.raises(ReviewConflict):
        journal.decision(row['id'], actor_id='manager', action='approve', revision=0)
    assert journal.get(row['id'])['revision'] == 0
    first = journal.decision(row['id'], actor_id='manager', action='confirm', revision=0)
    assert first['state'] == 'confirmed' and first['revision'] == 1
    with pytest.raises(ReviewConflict):
        journal.decision(row['id'], actor_id='manager', action='approve', revision=0)
    second = journal.decision(row['id'], actor_id='manager', action='approve', revision=1)
    assert second['state'] == 'approved'
    with journal._connect() as conn:
        rows = conn.execute('SELECT action,snapshot FROM "SyncFieldReviewHistory" ORDER BY id').fetchall()
    assert [entry['action'] for entry in rows] == ['confirm', 'approve']
    assert all(entry['snapshot'] == row['snapshot'] for entry in rows)


def test_real_restore_progress_and_recheck_retains_prior_snapshot(journal):
    row = request(journal)
    restored = journal.decision(row['id'], actor_id='manager', action='restore', revision=0, restore_from='zoho')
    assert restored['progress']['_restore']['value'] == '元値'
    journal.record_progress(row['id'], 'notion', {'desired': '元値', 'state': 'writing'})
    journal.finish(row['id'], 'failed', error='確認後に値が変更されました')
    failed = journal.get(row['id'])
    assert failed['progress']['notion']['state'] == 'writing'
    new_snapshot = {**row['snapshot'], 'zoho': {**row['snapshot']['zoho'], 'value': '別の新値', 'canonical': '別の新値'}}
    rechecked = journal.decision(row['id'], actor_id='manager', action='recheck', revision=failed['revision'], new_snapshot=new_snapshot)
    assert rechecked['state'] == 'pending'
    assert rechecked['snapshot'] == new_snapshot and rechecked['progress'] == {}
    with journal._connect() as conn:
        history = conn.execute('SELECT snapshot FROM "SyncFieldReviewHistory" WHERE action=%s', ('recheck',)).fetchone()
    assert history['snapshot'] == row['snapshot']


def test_source_observation_merges_fields_and_rejects_older_event(journal):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    args = ('client_master', 'page', 'kintone')
    assert journal.source_values(*args) == {}
    journal.observe_source(*args, {'取引先名': '元値', '有効': False}, now)
    journal.observe_source(*args, {'取引先名': ''}, now + timedelta(seconds=1))
    journal.observe_source(*args, {'取引先名': '古い値'}, now)
    assert journal.source_values(*args) == {'取引先名': '', '有効': False}
