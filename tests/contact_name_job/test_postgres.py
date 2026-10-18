"""使い捨てローカルDBで、実SQLとセッションロックを検証する。"""
import os
from pathlib import Path
from urllib.parse import urlparse, quote
from uuid import uuid4
import psycopg
from psycopg import sql
import pytest
from src.contact_name_job.repository import Repository
from tests.contact_name_job.test_job import entry


@pytest.fixture
def repo(monkeypatch):
    dsn=os.environ.get('EMAIL_REVIEW_TEST_DATABASE_URL')
    if not dsn:pytest.skip('使い捨てローカルDBを指定した場合のみ')
    assert urlparse(dsn).hostname in {'127.0.0.1','localhost'}
    assert urlparse(dsn).path=='/crm_email_review_test'
    schema='names_'+uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    scoped=dsn+'?options='+quote('-csearch_path='+schema)
    # optionsを接続側で指定するため、接続後のsearch_pathをテストだけで設定する。
    from psycopg.rows import dict_row
    def connect():
        return psycopg.connect(scoped,row_factory=dict_row,autocommit=True)
    monkeypatch.setattr('src.contact_name_job.repository.connect',connect)
    try:
        with connect() as c:c.execute(Path('dashboard/prisma/migrations/20261018000000_contact_name_job/migration.sql').read_text())
        yield Repository()
    finally:
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_import_paused_and_resume_progress(repo):
    e=entry();repo.import_plan([e],set())
    assert repo.status()['status']=='paused'
    repo.set_enabled(True)
    with repo.shard(0) as conn:
        assert repo.active(conn)
        assert repo.next(conn,0)['plan']==e
        repo.finish(conn,e,'verified')
    repo.import_plan([e],set())
    assert repo.status()['status']=='completed'
    assert repo.status()['counts']['verified']==1


def test_duplicate_shard_rejected_and_lock_released(repo):
    with repo.shard(0) as first:
        assert first is not None
        with repo.shard(0) as second:assert second is None
        with repo.shard(1) as other:assert other is not None
    with repo.shard(0) as next_run:assert next_run is not None


def test_retry_delay_and_terminal_attention(repo):
    e=entry();repo.import_plan([e],set());repo.set_enabled(True)
    with repo.shard(0) as conn:
        for _ in range(8):repo.finish(conn,e,'retry','ConnectionError')
        assert repo.next(conn,0) is None
    assert repo.status()['status']=='needs_attention'
    assert repo.status()['counts']['failed']==1


def test_changed_plan_and_foreign_verified_rejected(repo):
    e=entry();repo.import_plan([e],set())
    with pytest.raises(ValueError):repo.import_plan([entry()],set())
    with pytest.raises(ValueError):repo.import_plan([e],{str(uuid4())})


def test_full_size_import_and_four_parallel_finishes(repo):
    from concurrent.futures import ThreadPoolExecutor
    entries=[entry() for _ in range(33042)]
    repo.import_plan(entries,set())
    assert repo.status()['total']==33042
    repo.set_enabled(True)
    def finish_shard(shard):
        with repo.shard(shard) as conn:
            for _ in range(10):
                row=repo.next(conn,shard);repo.finish(conn,row['plan'],'verified')
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(finish_shard,range(4)))
    assert repo.status()['counts']['verified']==40


def test_rate_limit_does_not_exhaust_retry_attempts(repo):
    e=entry();repo.import_plan([e],set())
    with repo.shard(0) as conn:
        for _ in range(10):repo.finish(conn,e,'retry','NotionApiError',count_attempt=False)
        assert conn.execute('SELECT attempts FROM "ContactNameJobItem"').fetchone()['attempts']==0
    assert repo.status()['counts']['retry']==1


def test_server_expired_lock_blocks_old_owner_and_allows_new_owner(repo):
    import time
    with repo.shard(0) as old:
        old.execute('SET idle_session_timeout=100')
        time.sleep(0.2)
        with repo.shard(0) as new:assert new is not None
        with pytest.raises(psycopg.OperationalError):old.execute('SELECT 1')
