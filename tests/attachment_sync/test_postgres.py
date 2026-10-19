"""使い捨てローカルPostgresのみで台帳とロックを検証する。"""
import os
from pathlib import Path
from urllib.parse import urlparse, quote
from uuid import uuid4
import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from src.attachment_sync.repository import Repository
from tests.attachment_sync.test_sync import S


@pytest.fixture
def repo(monkeypatch):
    dsn=os.environ.get('EMAIL_REVIEW_TEST_DATABASE_URL')
    if not dsn:pytest.skip('ローカル使い捨てDBを指定してください')
    assert urlparse(dsn).hostname in {'127.0.0.1','localhost'}
    assert urlparse(dsn).path=='/crm_email_review_test'
    schema='attachments_'+uuid4().hex
    with psycopg.connect(dsn,autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    scoped=dsn+'?options='+quote('-csearch_path='+schema)
    def connect():return psycopg.connect(scoped,row_factory=dict_row,autocommit=True)
    monkeypatch.setattr('src.attachment_sync.repository.connect',connect)
    try:
        with connect() as conn:conn.execute(Path('dashboard/prisma/migrations/20261019090000_attachment_sync/migration.sql').read_text())
        yield Repository
    finally:
        with psycopg.connect(dsn,autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def test_global_lock_and_release(repo):
    with repo.locked() as first:
        assert first
        with repo.locked() as second:assert second is None
    with repo.locked() as third:assert third


def test_preserve_receipts_and_resume_held(repo):
    with repo.locked() as r:
        r.add(S);r.add(S)
        assert r.pending()['id']==S.key
        r.save_asset('asset',{'receipts':{'notion':{'state':'writing'}}})
        r.finish(S.key,'held','asset','要確認')
        assert r.pending() is None
        assert r.problems()[0]['reason']=='要確認'
        assert r.retry_held(S.key)==1
        assert r.pending()['assetId']=='asset'
        assert r.asset('asset')['receipts']['notion']['state']=='writing'
        r.mark_mirror(S,'asset')
        r.add(S)
        assert r.pending() is None
        assert r.status()['counts']=={'mirrored':1}


def test_new_event_during_discovery_is_not_lost(repo):
    with repo.locked() as r:
        key='event:action:page'
        r.put_state(key,{'version':'old'})
        old=r.next_event()
        r.put_state(key,{'version':'new'})
        r.complete_event(old)
        assert r.next_event()['data']['version']=='new'
        r.complete_event(r.next_event())
        assert r.next_event() is None
        r.put_state('error:action:page',{'reason':'確認待ち'})
        assert r.status()['discovery_error_count']==1
        r.put_state('error:action:page',{})
        assert r.status()['discovery_error_count']==0


def test_kintone_rotated_key_recognized_by_content_and_name(repo):
    from dataclasses import replace
    with repo.locked() as r:
        aid='a'*64
        r.save_asset(aid,{'db':'action','record':'page','sha256':'digest','name':'見積.pdf','receipts':{'kintone':{'state':'verified'}}})
        source=replace(S,tool='kintone',file_id='rotated-key',name='見積 [CRM-'+aid[:16]+'].pdf')
        assert r.find_mirror(source,'digest')==aid
        assert r.find_mirror(source,'changed') is None
        assert r.find_mirror(replace(source,record='other'),'digest') is None
        assert r.find_mirror(replace(source,name='original.pdf'),'digest') is None
