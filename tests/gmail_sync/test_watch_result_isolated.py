"""外部通信を禁止し、実際のSQLとGmail応答解釈を隔離環境で照合する。

SQLiteはPostgresの代替検証ではない。SQL保存件数とログの対応を検査する。
"""
from __future__ import annotations

import json
import logging
import socket
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import cron
from src.gmail_sync import db, watch_registration


class Cursor:
    def __init__(self, connection):
        self.cursor = connection.cursor()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, params=()):
        self.cursor.execute(sql.replace('%s', '?'), tuple(
            value.isoformat() if isinstance(value, datetime) else value for value in params
        ))

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def _row(self, row):
        if row is None:
            return None
        row = dict(row)
        for key in ('watchExpiration', 'lastSyncedAt'):
            if row.get(key):
                row[key] = datetime.fromisoformat(row[key])
        return row

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def fetchone(self):
        return self._row(self.cursor.fetchone())


class Connection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *args):
        if exc_type:
            self.connection.rollback()
        self.connection.close()

    def cursor(self):
        return Cursor(self.connection)

    def commit(self):
        self.connection.commit()


@pytest.fixture
def isolated(monkeypatch, tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    def forbidden(*args, **kwargs):
        raise AssertionError('隔離テストから外部通信は禁止')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setenv('CRON_SECRET', 'test-only')
    monkeypatch.delenv('CLOUD_RUN_SCHEDULER_AUTH_ENABLED', raising=False)
    monkeypatch.setenv('GMAIL_PUBSUB_TOPIC_NAME', 'projects/test/topics/test')
    monkeypatch.setenv('GOOGLE_OAUTH_CLIENT_ID', 'test-only')
    monkeypatch.setenv('GOOGLE_OAUTH_CLIENT_SECRET', 'test-only')
    monkeypatch.setattr(watch_registration, 'decrypt_token', lambda _: 'test-only')
    path = tmp_path / 'isolated.sqlite'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE "RepGmailConnection" ("repEmail" TEXT PRIMARY KEY, '
                           '"refreshTokenEnc" TEXT, "lastSyncedAt" TEXT, "historyId" TEXT, "watchExpiration" TEXT)')
    monkeypatch.setattr(db, '_connect', lambda: Connection(path))
    app = FastAPI()
    app.include_router(cron.router)
    client = TestClient(app)
    attempts = []
    responses = []
    expiration = datetime.now(timezone.utc) + timedelta(days=7)

    def request(method, url, **kwargs):
        response = requests.Response()
        response.status_code = 200
        if url == 'https://oauth2.googleapis.com/token':
            payload = {'access_token': 'test-only'}
        else:
            assert method == 'POST'
            assert url == 'https://gmail.googleapis.com/gmail/v1/users/me/watch'
            attempts.append(kwargs['json'])
            item = responses.pop(0) if responses else None
            if isinstance(item, BaseException):
                raise item
            if item == 'error':
                response.status_code = 400
                payload = {'error': {'message': 'private@example.invalid DUMMY_PRIVATE_VALUE'}}
            else:
                payload = {'historyId': '9999', 'expiration': str(int(expiration.timestamp() * 1000))}
        response._content = json.dumps(payload).encode()
        return response

    monkeypatch.setattr(requests, 'request', request)

    def seed(email, history='1000', due=True):
        with sqlite3.connect(path) as connection:
            connection.execute('INSERT INTO "RepGmailConnection" VALUES (?, ?, NULL, ?, ?)',
                               (email, 'test-only', history, None if due else expiration.isoformat()))

    return client, seed, attempts, responses, path


def records(capsys):
    output = capsys.readouterr().out
    return [json.loads(line) for line in output.splitlines() if line.startswith('{')]


def call(client):
    return client.get('/api/cron/gmail-watch-renewal', headers={'X-Cron-Secret': 'test-only'})


@pytest.mark.parametrize('partial', [False, True])
def test_http_counts_match_sql_and_google_attempts(isolated, capsys, caplog, partial):
    client, seed, attempts, responses, path = isolated
    seed('first@example.invalid', history=None)
    seed('second@example.invalid')
    seed('skip@example.invalid', due=False)
    if partial:
        responses.extend(['error', None])
    response = call(client)
    assert response.status_code == 200
    result = response.json()
    assert result['status'] == ('partial_failure' if partial else 'success')
    assert result['completed'] is True
    assert result['counts'] == {
        'total': 3, 'attempted': 2, 'renewed': 1 if partial else 2,
        'failed': 1 if partial else 0, 'skipped': 1, 'in_flight': 0,
        'remaining': 0, 'skip_reasons': {'not_due': 1},
    }
    assert len(attempts) == 2
    with sqlite3.connect(path) as connection:
        rows = connection.execute('SELECT "repEmail", "historyId", "watchExpiration" FROM "RepGmailConnection" ORDER BY "repEmail"').fetchall()
    assert rows[0][1] == (None if partial else '9999')
    assert (rows[0][2] is None) == partial
    assert rows[1][1] == '1000'
    assert rows[1][2] is not None
    logs = records(capsys)
    assert logs[-1] == result
    assert logs[0]['event'] == 'started'
    assert len({row['run_id'] for row in logs}) == 1
    assert [row['sequence'] for row in logs] == list(range(1, len(logs) + 1))
    assert datetime.fromisoformat(result['ended_at']) >= datetime.fromisoformat(result['started_at'])
    assert all(row['ended_at'] is None for row in logs[:-1])
    serialized = json.dumps(logs) + response.text + caplog.text
    for forbidden in ['example.invalid', 'DUMMY_PRIVATE_VALUE', 'test-only', '9999']:
        assert forbidden not in serialized


def test_cooperative_interrupt_preserves_confirmed_progress(isolated, capsys):
    _, seed, attempts, responses, path = isolated
    for n in range(3):
        seed(f'{n}@example.invalid')
    responses.extend([None, KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        cron.run_gmail_watch_renewal()
    result = records(capsys)[-1]
    assert result['status'] == 'interrupted'
    assert result['completed'] is False
    assert result['counts']['renewed'] == 1
    assert result['counts']['in_flight'] == 1
    assert result['counts']['remaining'] == 1
    assert len(attempts) == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT count(*) FROM "RepGmailConnection" WHERE "watchExpiration" IS NOT NULL').fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['empty', 'not_due', 'all_failed', 'not_configured', 'list_failed'])
def test_non_success_cases_are_explicit(isolated, monkeypatch, capsys, caplog, mode):
    client, seed, attempts, responses, _ = isolated
    expected_status = 'skipped'
    expected_http = 200
    if mode == 'not_due':
        seed('skip@example.invalid', due=False)
    elif mode == 'all_failed':
        seed('fail@example.invalid')
        responses.append('error')
        expected_status = 'failed'
    elif mode in ('not_configured', 'list_failed'):
        expected_status, expected_http = 'failed', 500
        if mode == 'not_configured':
            monkeypatch.delenv('GMAIL_PUBSUB_TOPIC_NAME')
        else:
            def fail():
                raise RuntimeError('private@example.invalid DUMMY_PRIVATE_VALUE')
            monkeypatch.setattr(db, 'list_gmail_connections', fail)
    response = call(client)
    assert response.status_code == expected_http
    logs = records(capsys)
    assert logs[-1]['status'] == expected_status
    if expected_http == 500:
        assert logs[-1]['counts']['total'] is None
        assert response.json()['detail'] == logs[-1]
    assert 'example.invalid' not in json.dumps(logs) + response.text + caplog.text
    if mode != 'all_failed':
        assert attempts == []


@pytest.mark.parametrize('initial', [False, True])
def test_zero_row_update_is_not_success(isolated, initial):
    expiration = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match='保存対象が1件'):
        if initial:
            db.update_watch_state('missing@example.invalid', '1000', expiration)
        else:
            db.update_watch_expiration('missing@example.invalid', expiration)


def test_auth_rejection_starts_no_work(isolated, capsys):
    client, _, attempts, _, _ = isolated
    assert client.get('/api/cron/gmail-watch-renewal').status_code == 401
    assert records(capsys) == []
    assert attempts == []


def test_hard_kill_leaves_unfinished_run_without_inventing_end_time(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys
    import time

    # 子プロセスは本番の環境変数を継承しない。通信も禁止し、2件目で待機する。
    script = r"""
import socket
import threading
from src.api.routes import cron
from src.gmail_sync import db, watch_registration

def forbidden(*args, **kwargs):
    raise AssertionError('network forbidden')
socket.socket.connect = forbidden
socket.create_connection = forbidden
db.list_gmail_connections = lambda: [db.RepGmailConnection(str(n), 'test', None) for n in range(3)]
watch_registration.decrypt_token = lambda _: 'test'
def renew(email, *args):
    if email == '1':
        threading.Event().wait()
watch_registration.register_or_renew_watch = renew
cron.run_gmail_watch_renewal()
"""
    root = Path(__file__).resolve().parents[2]
    log_path = tmp_path / 'process.jsonl'
    with log_path.open('w') as output:
        process = subprocess.Popen(
            [sys.executable, '-u', '-c', script], cwd=root,
            env={'PATH': os.defpath, 'PYTHONPATH': str(root), 'GMAIL_PUBSUB_TOPIC_NAME': 'test'},
            stdout=output, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 15
            logs = []
            while time.monotonic() < deadline:
                logs = [json.loads(line) for line in log_path.read_bytes().split(b'\n')[:-1]]
                if logs and logs[-1]['counts']['attempted'] == 2:
                    break
                assert process.poll() is None, '子プロセスが予定外に終了した'
                time.sleep(0.02)
            else:
                pytest.fail('2件目の開始記録が時間内に届かなかった')
            process.kill()
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            process.stderr.close()
    logs = [json.loads(line) for line in log_path.read_bytes().split(b'\n')[:-1]]
    assert logs[0]['event'] == 'started'
    assert all(row['event'] != 'finished' and row['ended_at'] is None for row in logs)
    assert logs[-1]['counts']['renewed'] == 1
    assert logs[-1]['counts']['in_flight'] == 1
    assert logs[-1]['counts']['remaining'] == 1


def test_commit_failure_is_counted_and_next_rep_continues(isolated, monkeypatch, capsys):
    client, seed, attempts, _, path = isolated
    seed('first@example.invalid')
    seed('second@example.invalid')
    original_commit = Connection.commit
    commits = 0

    def commit(self):
        nonlocal commits
        commits += 1
        if commits == 1:
            raise RuntimeError('private@example.invalid DUMMY_PRIVATE_VALUE')
        original_commit(self)

    monkeypatch.setattr(Connection, 'commit', commit)
    response = call(client)
    assert response.status_code == 200
    result = response.json()
    assert result['status'] == 'partial_failure'
    assert result['counts']['renewed'] == result['counts']['failed'] == 1
    assert len(attempts) == 2
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT count(*) FROM "RepGmailConnection" WHERE "watchExpiration" IS NOT NULL').fetchone()[0] == 1
    logs = records(capsys)
    assert logs[-1] == result
    assert 'example.invalid' not in json.dumps(logs) + response.text



def test_unexpected_abort_after_save_is_partial_failure(isolated, monkeypatch, capsys):
    client, seed, _, _, path = isolated
    seed('first@example.invalid')
    original = cron.renew_all_watches

    def abort(**kwargs):
        original(**kwargs)
        raise RuntimeError('private@example.invalid DUMMY_PRIVATE_VALUE')

    monkeypatch.setattr(cron, 'renew_all_watches', abort)
    response = call(client)
    assert response.status_code == 500
    result = response.json()['detail']
    assert result['status'] == 'partial_failure'
    assert result['reason'] == 'execution_failed'
    assert result['completed'] is False
    assert result['counts']['renewed'] == 1
    assert records(capsys)[-1] == result
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT count(*) FROM "RepGmailConnection" WHERE "watchExpiration" IS NOT NULL').fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['start', 'error_finish', 'progress', 'success_finish'])
def test_output_failure_is_safe_and_does_not_silently_continue(
    isolated, monkeypatch, capsys, caplog, mode,
):
    import builtins
    from src.infrastructure import cron_result_log

    client, seed, attempts, _, path = isolated
    seed('first@example.invalid')
    seed('second@example.invalid')
    real_print = builtins.print

    def fail_output(payload, **kwargs):
        record = json.loads(payload)
        should_fail = (
            mode == 'start'
            or (mode == 'error_finish' and record['event'] == 'finished')
            or (mode == 'success_finish' and record['event'] == 'finished')
            or (mode == 'progress' and record['counts']['renewed'] == 1)
        )
        if should_fail:
            raise OSError('private@example.invalid DUMMY_PRIVATE_VALUE')
        real_print(payload, **kwargs)

    monkeypatch.setattr(cron_result_log, 'print', fail_output, raising=False)
    if mode == 'error_finish':
        def fail_list():
            raise RuntimeError('private@example.invalid DUMMY_PRIVATE_VALUE')
        monkeypatch.setattr(db, 'list_gmail_connections', fail_list)
    response = call(client)
    assert response.status_code == 500
    result = response.json()['detail']
    assert result['log_write_failed'] is True
    expected_saved = {'start': 0, 'error_finish': 0, 'progress': 1, 'success_finish': 2}[mode]
    assert result['counts']['renewed'] == expected_saved
    assert len(attempts) == expected_saved
    if mode == 'progress':
        assert result['status'] == 'partial_failure'
        assert result['reason'] == 'log_write_failed'
        assert result['completed'] is False
    if mode == 'success_finish':
        # 業務走査は終了したが、終了ログは出せていないためHTTPは500。
        assert result['status'] == 'success'
        assert result['completed'] is True
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT count(*) FROM "RepGmailConnection" WHERE "watchExpiration" IS NOT NULL').fetchone()[0] == expected_saved
    captured = capsys.readouterr()
    combined = response.text + captured.out + captured.err + caplog.text
    for secret in ('example.invalid', 'DUMMY_PRIVATE_VALUE', 'test-only'):
        assert secret not in combined


def test_output_error_suppresses_original_exception_chain(monkeypatch):
    import traceback
    from src.infrastructure import cron_result_log

    marker = 'DUMMY_PRIVATE_VALUE'
    def fail_output(*args, **kwargs):
        raise OSError(marker)
    monkeypatch.setattr(cron_result_log, 'print', fail_output, raising=False)
    try:
        try:
            raise RuntimeError(marker)
        except RuntimeError:
            cron_result_log.WatchRenewalLog().emit('finished', status='failed')
    except cron_result_log.ResultLogWriteError as exc:
        formatted = ''.join(traceback.format_exception(exc))
        assert marker not in formatted
        assert 'During handling' not in formatted
    else:
        pytest.fail('出力失敗を検出しなかった')


def test_invalid_expiration_fails_one_rep_without_stopping_others(isolated, monkeypatch, capsys):
    client, seed, attempts, _, path = isolated
    seed('first@example.invalid')
    seed('second@example.invalid')
    original = db.list_gmail_connections

    def invalid_first():
        from dataclasses import replace
        rows = original()
        return [replace(rows[0], watch_expiration='private@example.invalid'), *rows[1:]]

    monkeypatch.setattr(db, 'list_gmail_connections', invalid_first)
    response = call(client)
    assert response.status_code == 200
    result = response.json()
    assert result['status'] == 'partial_failure'
    assert result['counts']['attempted'] == 2
    assert result['counts']['failed'] == result['counts']['renewed'] == 1
    assert result['counts']['remaining'] == result['counts']['in_flight'] == 0
    assert len(attempts) == 1
    assert records(capsys)[-1] == result
    with sqlite3.connect(path) as connection:
        assert connection.execute('SELECT count(*) FROM "RepGmailConnection" WHERE "watchExpiration" IS NOT NULL').fetchone()[0] == 1
