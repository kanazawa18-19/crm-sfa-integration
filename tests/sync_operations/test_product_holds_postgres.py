"""保留再開と履歴を、外部配送しないローカルDBで確認する。"""
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlparse
import os
import uuid

import psycopg
from psycopg import sql
import pytest
from src.sync_operations import product_holds as module
from src.sync_review.domain import ReviewConflict, ReviewForbidden


@pytest.fixture
def local(monkeypatch):
    dsn = os.environ.get('SYNC_REVIEW_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('明示したローカルDBのみ')
    assert urlparse(dsn).hostname in {'127.0.0.1', 'localhost'}
    assert urlparse(dsn).path == '/crm_field_review_test'
    schema = 'operations_' + uuid.uuid4().hex
    scoped = dsn + '?options=' + quote('-csearch_path=' + schema)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    monkeypatch.setenv('DATABASE_URL', scoped)
    @contextmanager
    def lock(*args):
        yield
    monkeypatch.setattr(module, 'acquire_record_sync_lock', lock)
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute('CREATE TABLE "User" (id TEXT PRIMARY KEY,"isManager" BOOLEAN NOT NULL)')
            conn.execute("INSERT INTO \"User\" VALUES ('manager',true),('viewer',false)")
            for migration in ('20260928010000_add_project_product_link_task', '20260928060000_sync_operation_history'):
                conn.execute((Path('dashboard/prisma/migrations') / migration / 'migration.sql').read_text())
            conn.execute('INSERT INTO "ProjectProductLinkTask" ("projectId","evaluationHeld") VALUES (%s,true)', ('p',))
            conn.execute('INSERT INTO "ProjectProductLinkPair" ("projectId","productId","clientId",state) VALUES (%s,%s,%s,%s)', ('p','product','client','held'))
            conn.execute('INSERT INTO "ProjectProductLinkDelivery" ("projectId","dbKey","notionId",state,"verificationAttempts") VALUES (%s,%s,%s,%s,8)', ('p','product','product','held'))
        yield SimpleNamespace(get=lambda _: SimpleNamespace(db_key='project'))
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_resume_checks_manager_hash_and_keeps_transactional_history(local):
    with pytest.raises(ReviewForbidden):
        module.list_product_holds('viewer')
    item = module.list_product_holds('manager')['items'][0]
    with pytest.raises(ReviewConflict):
        module.resume_product_hold(actor_id='manager', project_id='p', expected_hash='0'*64, store=local)
    with pytest.raises(ReviewForbidden):
        module.resume_product_hold(actor_id='viewer', project_id='p', expected_hash=item['hash'], store=local)
    result = module.resume_product_hold(actor_id='manager', project_id='p', expected_hash=item['hash'], store=local)
    assert result['state'] == 'pending'
    assert module.list_product_holds('manager')['items'] == []
    with module.connect() as conn:
        history = conn.execute('SELECT * FROM "SyncOperationHistory"').fetchone()
    assert history['actorId'] == 'manager'
    assert history['beforeState']['pairs'][0]['state'] == 'held'
    assert history['afterState']['pairs'][0]['state'] == 'pending'
    assert history['afterState']['deliveries'][0]['state'] == 'pending'
    assert history['afterState']['deliveries'][0]['verificationAttempts'] == 0
    with pytest.raises(ReviewConflict):
        module.resume_product_hold(actor_id='manager', project_id='p', expected_hash=item['hash'], store=local)


def test_history_failure_rolls_back_task_pair_and_delivery(local):
    item = module.list_product_holds('manager')['items'][0]
    with module.connect() as conn:
        conn.execute('ALTER TABLE "SyncOperationHistory" ADD CONSTRAINT synthetic_failure CHECK (false)')
    with pytest.raises(psycopg.errors.CheckViolation):
        module.resume_product_hold(actor_id='manager', project_id='p', expected_hash=item['hash'], store=local)
    with module.connect() as conn:
        after = module.read_product_hold(conn, 'p')
        assert conn.execute('SELECT COUNT(*) AS n FROM "SyncOperationHistory"').fetchone()['n'] == 0
    assert after == item['snapshot']
