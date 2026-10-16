"""Slackの本人取得と更新APIを正しい送信形式で呼ぶ。"""
from unittest.mock import Mock

import pytest

from src.slack_crm import slack_api


@pytest.mark.parametrize('method,payload,verb,argument', [
    ('users.info', {'user': 'U_TEST'}, 'get', 'params'),
    ('views.open', {'trigger_id': 'test', 'view': {}}, 'post', 'json'),
])
def test_request_format(monkeypatch, method, payload, verb, argument):
    monkeypatch.setenv('SLACK_BOT_TOKEN', 'test-token')
    response = Mock()
    response.json.return_value = {'ok': True}
    get = Mock(return_value=response)
    post = Mock(return_value=response)
    monkeypatch.setattr(slack_api.requests, 'get', get)
    monkeypatch.setattr(slack_api.requests, 'post', post)
    assert slack_api.call(method, payload) == {'ok': True}
    selected, other = (get, post) if verb == 'get' else (post, get)
    selected.assert_called_once_with(
        'https://slack.com/api/' + method,
        **{argument: payload},
        headers={'Authorization': 'Bearer test-token'}, timeout=(0.4, 1.5),
    )
    other.assert_not_called()
