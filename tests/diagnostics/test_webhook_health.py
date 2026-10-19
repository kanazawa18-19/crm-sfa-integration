"""購読状態と未着の判定を実例・境界値で確かめる。"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.diagnostics.webhook_health import DEFAULT_MODULES, evaluate_delivery, evaluate_watch, utc
from src.api.routes import diagnostics

NOW = datetime(2026, 9, 27, 9, tzinfo=timezone.utc)
CALLBACK = 'https://example.test/api/webhooks/zoho'


@pytest.mark.parametrize('age,status', [(1799, 'ok'), (1800, 'ok'), (1801, 'warning'), (-1, 'unknown'), (None, 'unknown')])
def test_Gmail観測完了の停滞(age, status):
    from src.diagnostics.webhook_health import evaluate_gmail_observation
    last = NOW - timedelta(seconds=age) if age is not None else None
    assert evaluate_gmail_observation(last, now=NOW)['status'] == status


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


def test_未完了の古いイベント時刻だけで異常と断定しない():
    from src.diagnostics.unfinished_sync import summarize
    rows=[{'dbKey':'project','notionKey':'a','acceptedAt':NOW-timedelta(days=20)}]
    first=summarize(rows,NOW)
    second=summarize(rows+[{'dbKey':'action','notionKey':'b','acceptedAt':NOW}],NOW+timedelta(hours=1))
    assert first['status']=='pending'
    assert first['activity_at']==NOW.isoformat()
    assert first['fingerprint']==second['fingerprint']
    assert second['by_database']=={'project':1,'action':1}
    assert summarize([],NOW)['status']=='ok'


def test_項目別未完了を内部基準行と区別して集計する():
    from src.diagnostics.unfinished_sync import summarize
    base = {'dbKey': 'client_master', 'notionKey': 'a', 'acceptedAt': NOW}
    result = summarize([base, dict(base, propertyName='TEL'),
                        dict(base, propertyName='住所'),
                        dict(base, propertyName='__baseline__')], NOW)
    assert result['count'] == 3
    assert result['record_count'] == 1
    assert result['field_count'] == 2
    assert result['legacy_count'] == 1
    assert summarize([dict(base, propertyName='__baseline__')], NOW)['status'] == 'ok'


def test_同一レコード同時刻でも別項目の滞留は識別する():
    from src.diagnostics.unfinished_sync import summarize
    base = {'dbKey': 'client_master', 'notionKey': 'a', 'acceptedAt': NOW}
    first = summarize([dict(base, propertyName='TEL')], NOW)
    second = summarize([dict(base, propertyName='住所')], NOW)
    assert first['fingerprint'] != second['fingerprint']


def test_対象外の旧受理履歴を再送待ちと区別する():
    from src.diagnostics.unfinished_sync import summarize
    row = {'dbKey': 'client_master', 'notionKey': '3d8d8ea8-d4f3-817d-a1c8-f30516aacc8d', 'acceptedAt': NOW}
    result = summarize([row], NOW)
    assert result['status'] == 'ok'
    assert result['count'] == 0
    assert result['excluded_legacy_count'] == 1
    assert result['exclusions_by_decision'] == {'Q052': 1}
    assert summarize([dict(row, propertyName='TEL')], NOW)['count'] == 1
