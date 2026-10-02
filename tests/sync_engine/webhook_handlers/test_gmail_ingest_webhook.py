"""GAS取込みの認証・比較・再送時の入口を確認する。"""
import json

from src.sync_engine.webhook_handlers import gmail_ingest_webhook as ingest


def _event(dry_run=True):
    return {'headers': {'X-Webhook-Secret': 'test-secret'}, 'body': json.dumps({
        'rep_email': 'rep@example.com', 'dry_run': dry_run,
        'messages': [{'id': 'm1', 'from': 'buyer@customer.com', 'to': 'rep@example.com',
                      'subject': '相談', 'snippet': '本文', 'internal_date_ms': '1780000000000'}],
    })}


def test_認証と担当者を本文だけで偽装できない(monkeypatch):
    monkeypatch.setenv('GMAIL_INGEST_WEBHOOK_SECRET', 'test-secret')
    monkeypatch.setenv('GMAIL_INGEST_REP_EMAIL', 'rep@example.com')
    event = _event()
    event['headers'] = {}
    assert ingest.handler(event, None)['statusCode'] == 401
    event = _event()
    body = json.loads(event['body'])
    body['rep_email'] = 'other@example.com'
    event['body'] = json.dumps(body)
    assert ingest.handler(event, None)['statusCode'] == 400


def test_dry_runは保存せずID別に比較できる(monkeypatch):
    monkeypatch.setenv('GMAIL_INGEST_WEBHOOK_SECRET', 'test-secret')
    monkeypatch.setenv('GMAIL_INGEST_REP_EMAIL', 'rep@example.com')
    monkeypatch.setattr(ingest.db, 'email_log_exists', lambda _id: False)
    monkeypatch.setattr(ingest, 'find_contact_page_id', lambda _client, _address: 'contact-1')
    monkeypatch.setattr(ingest.sync, 'record_message', lambda *a, **k: (_ for _ in ()).throw(AssertionError('保存した')))
    result = ingest.handler(_event(), None, contact_client=object())
    assert result['statusCode'] == 200
    body = json.loads(result['body'])
    assert body['results'] == [{'id': 'm1', 'status': 'would_insert'}]


def test_実取込みは共通保存関数へ渡す(monkeypatch):
    monkeypatch.setenv('GMAIL_INGEST_WEBHOOK_SECRET', 'test-secret')
    monkeypatch.setenv('GMAIL_INGEST_REP_EMAIL', 'rep@example.com')
    monkeypatch.setattr(ingest.db, 'email_log_exists', lambda _id: False)
    calls = []
    monkeypatch.setattr(ingest.sync, 'record_message', lambda *a, **k: calls.append(k) or True)
    result = ingest.handler(_event(False), None, contact_client=object())
    assert result['statusCode'] == 200
    assert calls[0]['atomic_insert'] is True


def test_1通の後処理失敗でも後続メールを受け付ける(monkeypatch):
    monkeypatch.setenv('GMAIL_INGEST_WEBHOOK_SECRET', 'test-secret')
    monkeypatch.setenv('GMAIL_INGEST_REP_EMAIL', 'rep@example.com')
    monkeypatch.setattr(ingest.db, 'email_log_exists', lambda _id: False)
    def record(message, *_args, **_kwargs):
        if message.id == 'm1':
            raise RuntimeError('Notionが応答しません')
        return True
    monkeypatch.setattr(ingest.sync, 'record_message', record)
    event = _event(False)
    body = json.loads(event['body'])
    body['messages'].append({**body['messages'][0], 'id': 'm2'})
    event['body'] = json.dumps(body)
    response = ingest.handler(event, None, contact_client=object())
    assert response['statusCode'] == 200
    assert [item['status'] for item in json.loads(response['body'])['results']] == [
        'effect_failed', 'inserted',
    ]


def test_長い宛先を切り捨てず受け取り上限超過は拒否する(monkeypatch):
    monkeypatch.setenv('GMAIL_INGEST_WEBHOOK_SECRET', 'test-secret')
    monkeypatch.setenv('GMAIL_INGEST_REP_EMAIL', 'rep@example.com')
    captured = []
    monkeypatch.setattr(ingest, '_process_one', lambda message, *args: captured.append(message.to_header) or 'unmatched')
    for length, expected in [(6213, 200), (65536, 200), (65537, 400)]:
        event = _event()
        body = json.loads(event['body'])
        header = 'a' * length
        body['messages'][0]['to'] = header
        event['body'] = json.dumps(body)
        previous = len(captured)
        response = ingest.handler(event, None, contact_client=object())
        assert response['statusCode'] == expected
        if expected == 200:
            assert captured[-1] == header
        else:
            assert len(captured) == previous
