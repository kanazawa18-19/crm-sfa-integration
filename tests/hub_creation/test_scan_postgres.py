"""本番接続を拒否した実PostgreSQLで、しおりと作成予約の境界を確認する。"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote, urlparse
import os
import threading
import uuid
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from src.hub_creation.journal import PostgresCreationJournal, connect
from src.hub_creation.scan_journal import PostgresScanJournal
from src.sync_engine.record_sync_lock import acquire_record_sync_lock, RecordSyncBusy


@pytest.fixture
def local(monkeypatch):
    dsn = os.environ.get('SYNC_REVIEW_TEST_DATABASE_URL')
    if not dsn: pytest.skip('明示したローカルDBのみ')
    assert urlparse(dsn).hostname in {'127.0.0.1','localhost'}
    assert urlparse(dsn).path == '/crm_field_review_test'
    schema='scan_'+uuid.uuid4().hex
    scoped=dsn+'?options='+quote('-csearch_path='+schema)
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    monkeypatch.setenv('DATABASE_URL',scoped)
    monkeypatch.setenv('DATABASE_URL_UNPOOLED',scoped)
    # 本番ヘルパーのtimezone指定がsearch_pathを上書きするため、テスト区画を接続時に固定する。
    monkeypatch.setattr('src.db_utils.connect_for_advisory_lock',
                        lambda logger: psycopg.connect(scoped,row_factory=dict_row))
    try:
        with psycopg.connect(scoped) as conn:
            for name in ('20260928000000_add_hub_creation_attempt','20260928130000_hub_creation_scan'):
                conn.execute((Path('dashboard/prisma/migrations')/name/'migration.sql').read_text())
            conn.execute('''CREATE TABLE "RecordSyncWatermark" ("dbKey" TEXT,"notionKey" TEXT,
                "acceptedAt" TIMESTAMPTZ,"completedAt" TIMESTAMPTZ,PRIMARY KEY("dbKey","notionKey"))''')
        yield PostgresScanJournal(),PostgresCreationJournal()
    finally:
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


CONTEXT={'sourceKey':'notion:page','sourceId':'page','dbKey':'chain'}


def make_due():
    with connect() as conn:
        conn.execute('UPDATE "HubCreationScan" SET "nextAttemptAt"=now()-interval \'1 minute\'')


def test_resume_cursor_and_unknown_post_never_requeued(local):
    scans,attempts=local
    scans.save(CONTEXT,'hash',{'phase':'full','last_id':'2000'})
    assert PostgresScanJournal().get('notion:page')['checkpoint']['last_id']=='2000'
    make_due(); assert len(scans.due())==1
    assert attempts.reserve('notion:page','zoho','chain','identity')
    assert scans.due()==[]
    assert not attempts.reserve('notion:page','zoho','chain','identity')
    attempts.finish('notion:page','zoho','external')
    assert scans.due()==[]


def test_candidate_hold_stops_worker_until_explicit_notification(local):
    scans,_=local
    scans.save(CONTEXT,'hash',{'phase':'full','last_id':'2000'},state='held',reason='比較待ち')
    make_due(); assert scans.due()==[]
    scans.save(CONTEXT,'new-input',{'phase':'full','last_id':'0'})
    make_due(); assert len(scans.due())==1
    assert scans.get('notion:page')['inputHash']=='new-input'


def test_same_identity_two_sources_only_one_post_reservation(local):
    _,attempts=local
    barrier=threading.Barrier(2)
    def reserve(index):
        barrier.wait()
        return attempts.reserve('notion:'+str(index),'zoho','chain','same-identity')
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve,range(2)))==[False,True]


def test_cursor_writers_share_real_source_lock(local):
    scans,_=local
    entered=threading.Event(); release=threading.Event()
    def first():
        with acquire_record_sync_lock(object(),'chain','notion:page'):
            scans.save(CONTEXT,'hash',{'last_id':'2000'})
            entered.set(); assert release.wait(5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running=pool.submit(first)
        if not entered.wait(5):
            running.result()
            pytest.fail('先行処理が開始しませんでした')
        try:
            with pytest.raises(RecordSyncBusy):
                with acquire_record_sync_lock(object(),'chain','notion:page'):
                    scans.save(CONTEXT,'hash',{'last_id':'0'})
        finally:
            release.set()
        running.result()
    assert scans.get('notion:page')['checkpoint']['last_id']=='2000'
