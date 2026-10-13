"""自然観測の永続証拠と再送境界を検証する。"""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest
from src.gmail_sync import observation


@pytest.fixture
def local(monkeypatch):
    dsn = os.environ.get('SYNC_REVIEW_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('明示したローカルDBのみ')
    assert urlparse(dsn).hostname in {'127.0.0.1', 'localhost'}
    assert urlparse(dsn).path == '/crm_field_review_test'
    schema = 'gmail_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    def connect():
        return psycopg.connect(dsn, row_factory=dict_row, options=f'-csearch_path={schema}')
    monkeypatch.setattr(observation.db, '_connect', connect)
    with connect() as conn:
        conn.execute(Path('dashboard/prisma/migrations/20261014000000_gmail_ingest_observation/migration.sql').read_text())
    yield connect
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def meta(phase, seq=0, listed=0):
    return dict(scan_id='test-scan-identifier-0001', phase=phase, kind='natural',
                after=1791800000, before=1791807200, sequence=seq, listed=listed)


def send(phase, seq=0, listed=0, messages=None, process=None):
    return observation.record(meta(phase, seq, listed), rep_email='rep@example.com',
                              dry_run=True, messages=messages or [],
                              process=process or (lambda: dict(received=0, dry_run=True, results=[])))


def message(id='m1'):
    return SimpleNamespace(id=id, internal_date_ms='1791800100000')


def response(status='existing'):
    return dict(received=1, dry_run=True, results=[dict(id='m1', status=status)])


def test_未開始と欠けた連番の終端は拒否(local):
    with pytest.raises(ValueError, match='開始記録'):
        send('batch')
    send('started')
    send('batch', seq=1, messages=[message()], process=response)
    with pytest.raises(ValueError, match='全バッチ'):
        send('complete', seq=2, listed=1)
    with local() as conn:
        assert conn.execute('SELECT count(*) n FROM "GmailIngestObservation" WHERE phase=\'complete\'').fetchone()['n'] == 0


def test_同時再送は判定を一度だけ実行し保存応答を返す(local):
    send('started')
    calls = []
    def process():
        calls.append(1)
        return response()
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(send, 'batch', messages=[message()], process=process) for _ in range(2)]
        assert all(f.result()['observation_saved'] for f in futures)
    assert len(calls) == 1
    send('complete', seq=1, listed=1)
    assert send('complete', seq=1, listed=1)['observation_saved']
    with local() as conn:
        rows = conn.execute('SELECT * FROM "GmailIngestObservation"').fetchall()
    assert len(rows) == 3
    serialized = json.dumps(rows, default=str)
    assert 'snippet' not in serialized and 'subject' not in serialized and 'from_header' not in serialized
    assert all(row['receivedAt'].tzinfo for row in rows)


def test_同じIDの条件変更は拒否し判定失敗は保存しない(local):
    send('started')
    with pytest.raises(RuntimeError):
        send('batch', messages=[message()], process=lambda: (_ for _ in ()).throw(RuntimeError()))
    send('batch', messages=[message()], process=response)
    with pytest.raises(ValueError, match='内容が競合'):
        send('batch', messages=[message('different')], process=response)
    with pytest.raises(ValueError, match='全バッチ'):
        send('complete', seq=1, listed=2)


def test_効果失敗のバッチも事実を保持し完走と成功を混同しない(local):
    send('started')
    send('batch', messages=[message()], process=lambda: response('effect_failed'))
    send('complete', seq=1, listed=1)
    with local() as conn:
        row = conn.execute('SELECT evidence FROM "GmailIngestObservation" WHERE phase=\'batch\'').fetchone()
    assert row['evidence']['results'][0]['status'] == 'effect_failed'


def test_補完走査の本取込みを拒否し未対応は区別する():
    m = meta('started');m['kind'] = 'backfill'
    with pytest.raises(ValueError, match='dry-run専用'):
        observation.validate(m, dry_run=False, messages=[])
    assert observation.validate(None, dry_run=True, messages=[]) is None
    assert observation.validate(m, dry_run=True, messages=[])['kind'] == 'backfill'


def test_ページ間でIDが再出現しても重複を証跡に残して終端を保存する(local):
    send('started')
    send('batch', messages=[message()], process=response)
    send('batch', seq=1, messages=[message()], process=response)
    send('complete', seq=2, listed=2)
    with local() as conn:
        row = conn.execute('SELECT evidence FROM "GmailIngestObservation" WHERE phase=\'complete\'').fetchone()
    assert row['evidence']['uniqueListed'] == 1
    assert row['evidence']['duplicateOccurrences'] == {'m1': 2}


def test_消失IDは本文なしの証跡として冪等保存し完走にしない(local):
    send('started')
    value = dict(meta('missing'), missing_ids=['gone'])
    value = observation.validate(value, dry_run=True, messages=[])
    for _ in range(2):
        observation.record(value, rep_email='rep@example.com', dry_run=True, messages=[],
                           process=lambda: pytest.fail('消失通知でメールを処理した'))
    with pytest.raises(ValueError, match='消失'):
        send('complete')
    with local() as conn:
        rows = conn.execute('SELECT evidence FROM "GmailIngestObservation" WHERE phase=\'missing\'').fetchall()
    assert len(rows) == 1 and rows[0]['evidence']['missingIds'] == ['gone']


def test_消失通知の応答紛失後に先行IDも消えても同じ連番で両IDを保存できる(local):
    send('started')
    for id in ['second-gone', 'first-gone', 'first-gone', 'second-gone']:
        value = observation.validate(dict(meta('missing'), missing_ids=[id]), dry_run=True, messages=[])
        observation.record(value, rep_email='rep@example.com', dry_run=True, messages=[], process=None)
    with local() as conn:
        rows = conn.execute('SELECT evidence FROM "GmailIngestObservation" WHERE phase=\'missing\'').fetchall()
    assert len(rows) == 2
    assert {row['evidence']['missingIds'][0] for row in rows} == {'first-gone', 'second-gone'}
