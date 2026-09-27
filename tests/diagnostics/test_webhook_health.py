"""購読状態と未着の判定を実例・境界値で確かめる。"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.diagnostics.webhook_health import DEFAULT_MODULES, evaluate_delivery, evaluate_watch, utc
from src.api.routes import diagnostics

NOW = datetime(2026, 9, 27, 9, tzinfo=timezone.utc)
CALLBACK = 'https://example.test/api/webhooks/zoho'


def watch(hours=12):
    return {'watch': [{'channel_id': '123', 'notify_url': CALLBACK,
                       'channel_expiry': (NOW + timedelta(hours=hours)).isoformat(),
                       'events': [f'{m}.{e}' for m in DEFAULT_MODULES for e in ('create', 'edit', 'delete')]}]}


@pytest.mark.parametrize('hours,status', [(12, 'ok'), (6, 'warning'), (0, 'critical'), (-1, 'critical')])
def test_購読期限(hours, status):
    assert evaluate_watch(watch(hours), channel_id='123', callback=CALLBACK, now=NOW)['status'] == status


def test_一部モジュール欠落も検知():
    body = watch(); body['watch'][0]['events'].pop()
    assert evaluate_watch(body, channel_id='123', callback=CALLBACK, now=NOW)['status'] == 'critical'


@pytest.mark.parametrize('body,status', [({}, 'unknown'), ({'watch': []}, 'critical'), ({'watch': [None]}, 'unknown'), ({'watch': [], 'info': {'more_records': True}}, 'unknown')])
def test_不明を消滅と混同しない(body, status):
    assert evaluate_watch(body, channel_id='123', callback=CALLBACK, now=NOW)['status'] == status


def test_異なるチャンネルでは正常にならない():
    assert evaluate_watch(watch(), channel_id='999', callback=CALLBACK, now=NOW)['status'] == 'critical'


def test_通知先不一致():
    assert evaluate_watch(watch(), channel_id='123', callback='https://wrong.test', now=NOW)['status'] == 'critical'


def test_無受信でも変更なしなら異常にしない():
    assert evaluate_delivery('kintone', NOW - timedelta(days=100), None, now=NOW)['status'] == 'ok'
    assert evaluate_delivery('spreadsheet', None, None, now=NOW)['status'] == 'unverified'


def test_変更後の未着は連続観測へ():
    assert evaluate_delivery('notion', NOW - timedelta(days=1), NOW - timedelta(hours=3), now=NOW)['status'] == 'pending'
    assert evaluate_delivery('notion', NOW, None, now=NOW, complete=False)['status'] == 'unknown'


def test_DB日時はUTC():
    assert utc(datetime(2026, 9, 27, 9)) == NOW


def test_専用認証が未設定や誤りなら観測自体を呼ばない(monkeypatch):
    app = FastAPI(); app.include_router(diagnostics.router)
    monkeypatch.delenv('WEBHOOK_HEALTH_TOKEN', raising=False)
    client = TestClient(app)
    assert client.get('/api/diagnostics/webhook-health').status_code == 401
    monkeypatch.setenv('WEBHOOK_HEALTH_TOKEN', 'test-only')
    assert client.get('/api/diagnostics/webhook-health', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    from src.diagnostics import webhook_health
    monkeypatch.setattr(webhook_health, 'collect_webhook_health', lambda: {'schema_version': 1})
    assert client.get('/api/diagnostics/webhook-health', headers={'Authorization': 'Bearer test-only'}).json() == {'schema_version': 1}


def test_購読クライアントの公開インターフェースで呼ぶ(monkeypatch):
    from src.diagnostics import webhook_health as h
    monkeypatch.setenv('ZOHO_WATCH_CHANNEL_ID', '123')
    monkeypatch.setenv('ZOHO_WEBHOOK_BASE_URL', 'https://example.test')
    class Client:
        def __init__(self, **kwargs): pass
        def request(self, method, url, *, json_body=None, idempotent=True):
            assert method == 'GET' and url.endswith('?channel_id=123')
            class Response:
                status_code = 200
                def json(self): return watch()
            return Response()
    monkeypatch.setattr(h, 'HttpZohoClient', Client)
    assert h._read_watch(NOW)['status'] == 'ok'
