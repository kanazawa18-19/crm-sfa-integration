"""使い捨てschemaで本物のSQL・排他・再評価を検証する。"""
import os
from pathlib import Path
from urllib.parse import urlparse, quote
from uuid import uuid4
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from src.contact_affiliation_job.repository import Repository


@pytest.fixture
def repo(monkeypatch):
    dsn = os.environ.get('EMAIL_REVIEW_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('使い捨てローカルDBを指定した場合のみ')
    assert urlparse(dsn).hostname in {'127.0.0.1', 'localhost'}
    assert urlparse(dsn).path == '/crm_email_review_test'
    schema = 'affiliation_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    scoped = dsn + '?options=' + quote('-csearch_path=' + schema)
    def connect():
        return psycopg.connect(scoped, row_factory=dict_row, autocommit=True)
    monkeypatch.setattr('src.contact_affiliation_job.repository.connect', connect)
    try:
        with connect() as conn:
            conn.execute(Path('dashboard/prisma/migrations/20261019060000_contact_affiliation_job/migration.sql').read_text())
        yield Repository()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_defaults_lock_cursor_and_progress(repo):
    assert repo.status()['enabled'] is False
    assert repo.status()['dry_run'] is True
    with repo.locked() as conn:
        with repo.locked() as other:
            assert other is None
        repo.discovery(conn, [{'id': 'one'}, {'id': 'two'}], {'cursor': 'abc'})
        repo.save(conn, 'one', 'delivery_pending', {'company_id': 'target'})
        repo.discovery(conn, [{'id': 'one'}], {'cursor': 'def'})
        assert repo.next(conn)['notionId'] == 'two'
    status = repo.status()
    assert status['counts'] == {'pending': 1, 'delivery_pending': 1}
    assert status['discovery'] == {'cursor': 'def'}


def test_enable_wakes_preview_only(repo):
    with repo.locked() as conn:
        repo.discovery(conn, [{'id': 'one'}, {'id': 'two'}], {})
        repo.save(conn, 'one', 'candidate', {})
        repo.save(conn, 'two', 'verified', {})
        assert repo.next(conn) is None
    repo.configure(enabled=True, dry_run=False)
    with repo.locked() as conn:
        assert repo.next(conn)['notionId'] == 'one'


def test_original_evidence_immutable_and_idempotent(repo):
    row = {'source': 'inquiry_form', 'source_id': 'form:1', 'origin': 'form:original-system',
           'email': 'test@example.invalid', 'company_name': '試験株式会社', 'explicit': True,
           'observed_at': '2026-10-19T00:00:00Z', 'source_url': 'https://example.invalid/forms/1'}
    repo.import_evidence([row])
    repo.import_evidence([row])
    assert repo.extra_evidence(row['email']) == [row]
    with pytest.raises(ValueError):
        repo.import_evidence([{**row, 'company_name': '別の株式会社'}])


def test_new_mail_prioritizes_contact_before_full_scan(repo):
    from psycopg.types.json import Jsonb
    key = str(uuid4())
    with repo.locked() as conn:
        conn.execute('CREATE TABLE "EmailReview" (id TEXT, data JSONB, "updatedAt" TIMESTAMPTZ)')
        conn.execute('CREATE TABLE "GmailContactEmail" ("pageId" TEXT, email TEXT)')
        conn.execute('INSERT INTO "GmailContactEmail" VALUES (%s,%s)', (key, 'person@example.invalid'))
        conn.execute('INSERT INTO "EmailReview" VALUES (%s,%s,now())',
                     ('mail', Jsonb({'participants': [{'email': 'person@example.invalid'}]})))
        assert repo.prioritize_mail(conn) == 1
        assert repo.next(conn)['notionId'] == key
        assert repo.prioritize_mail(conn) == 0


def test_verified_kept_out_of_daily_reassessment(repo):
    with repo.locked() as conn:
        repo.discovery(conn, [{'id': 'one'}], {})
        repo.save(conn, 'one', 'verified', {'company_id': 'target'})
        conn.execute("""UPDATE \"ContactAffiliationItem\" SET \"nextAttemptAt\"=now()-interval '2 days'""")
        assert repo.next(conn) is None
    assert repo.status()['counts']['verified'] == 1


def test_known_hold_continues_to_other_candidate(repo, monkeypatch):
    from src.contact_affiliation_job import worker
    repo.configure(enabled=True, dry_run=True)
    monkeypatch.setattr(repo, 'prioritize_mail', lambda conn: 0)
    class Gateway:
        def discover(self, cursor):
            return [{'id': 'a'}, {'id': 'b'}], {}
    def process(item, repository, conn, gateway, dry_run):
        if item['notionId'] == 'a':
            raise worker.DecisionHold('同じメールの連絡先が一意ではありません')
        repository.save(conn, item['notionId'], 'existing', {})
        return 'existing'
    monkeypatch.setattr(worker, 'process', process)
    result = worker.run(repository=repo, gateway=Gateway())
    assert result['counts'] == {'held': 1, 'existing': 1}
    assert repo.status()['retry_count'] == 0
    assert repo.status()['metrics']['last_evaluated'] == 2


@pytest.mark.parametrize('previous', ['existing', 'verified', 'released'])
def test_removed_affiliation_is_held_even_with_new_mail(repo, previous):
    from psycopg.types.json import Jsonb
    key = str(uuid4())
    with repo.locked() as conn:
        repo.discovery(conn, [{'id': key}], {})
        repo.save(conn, key, previous, {'reason': '以前の所属'})
        repo.discovery(conn, [{'id': key, 'properties': {'取引先マスター': {'type': 'relation', 'relation': []}}}], {})
        conn.execute('CREATE TABLE "EmailReview" (id TEXT, data JSONB, "updatedAt" TIMESTAMPTZ)')
        conn.execute('CREATE TABLE "GmailContactEmail" ("pageId" TEXT, email TEXT)')
        conn.execute('INSERT INTO "GmailContactEmail" VALUES (%s,%s)', (key, 'person@example.invalid'))
        conn.execute('INSERT INTO "EmailReview" VALUES (%s,%s,now())', ('mail', Jsonb({'participants': [{'email': 'person@example.invalid'}]})))
        conn.execute('UPDATE \"ContactAffiliationItem\" SET \"nextAttemptAt\"=now()-interval \'8 days\'')
        repo.prioritize_mail(conn)
        assert repo.next(conn) is None
    assert repo.status()['counts'] == {'released': 1}
    assert repo.report('history', key)[0]['state'] == 'released'


def test_held_weekly_and_discovery_preserves_evaluation_metrics(repo, monkeypatch):
    from src.contact_affiliation_job import worker
    repo.configure(enabled=True, dry_run=True)
    with repo.locked() as conn:
        repo.discovery(conn, [{'id': 'a'}], {})
        repo.save(conn, 'a', 'held', {'reason': '根拠不足'})
        delay = conn.execute('SELECT EXTRACT(EPOCH FROM ("nextAttemptAt"-now())) AS seconds FROM "ContactAffiliationItem"').fetchone()['seconds']
        assert 604700 < delay <= 604800
        conn.execute("""UPDATE \"ContactAffiliationJob\" SET metrics='{"evaluated_per_minute":12,"last_evaluated":3}'::jsonb""")
    monkeypatch.setattr(repo, 'prioritize_mail', lambda conn: 0)
    class Gateway:
        def discover(self, cursor): return [], {}
    worker.run(repository=repo, gateway=Gateway(), discover_only=True)
    assert repo.status()['metrics']['evaluated_per_minute'] == 12
