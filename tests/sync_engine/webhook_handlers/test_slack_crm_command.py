"""CRM検索は署名と本人DMを満たす場合だけ実行する。"""
import json
from src.sync_engine.webhook_handlers import slack_crm_command as command


def test_本人DMだけ検索する(monkeypatch):
    monkeypatch.setattr(command, '_verify_slack_signature', lambda *_: True)
    monkeypatch.setenv('SLACK_CRM_ALLOWED_USER_IDS', 'U123')
    monkeypatch.setenv('SLACK_CRM_ALLOWED_TEAM_ID', 'T123')
    calls = []
    def search(query):
        calls.append(query)
        return {'clients': [{'取引先名': 'テスト社'}], 'truncated': False}
    event = {'headers': {}, 'body': 'user_id=U123&team_id=T123&command=%2Fcrm&channel_id=C123&text=%E3%83%86%E3%82%B9%E3%83%88'}
    assert '登録済み' in json.loads(command.handler(event, None, search=search)['body'])['text']
    assert not calls
    event['body'] = event['body'].replace('C123', 'D123')
    assert 'テスト社' in json.loads(command.handler(event, None, search=search)['body'])['text']
    assert calls == ['テスト']


def test_署名不正は拒否する(monkeypatch):
    monkeypatch.setattr(command, '_verify_slack_signature', lambda *_: False)
    assert command.handler({'headers': {}, 'body': ''}, None)['statusCode'] == 401
