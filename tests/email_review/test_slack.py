"""署名・本人確認・候補検索のSlack境界を合成リクエストで検証。"""
import json
from urllib.parse import urlencode
from unittest.mock import Mock
import pytest
from src.email_review import slack
from src.email_review.domain import Forbidden
from src.slack_crm import handler


def test_signature_is_checked_before_email_dispatch(monkeypatch):
    monkeypatch.setattr(handler, '_verify_slack_signature', lambda *args: False)
    target = Mock()
    monkeypatch.setattr(slack, 'handle', target)
    event = {'headers': {}, 'body': urlencode({'payload': json.dumps({'type': 'block_actions', 'actions': [{'action_id': 'crm_email_home'}]})})}
    assert handler.handler(event)['statusCode'] == 401
    target.assert_not_called()


def test_workspace_and_guest_are_rejected(monkeypatch):
    monkeypatch.setenv('SLACK_CRM_ALLOWED_TEAM_ID', 'T1')
    repo = Mock()
    with pytest.raises(Forbidden):
        slack.actor({'team': {'id': 'other'}, 'user': {'id': 'U1'}}, repo)
    monkeypatch.setattr(slack, 'call', lambda *args: {'user': {'id': 'U1', 'is_restricted': True, 'profile': {'email': 'a@example.test'}}})
    with pytest.raises(Forbidden):
        slack.actor({'team': {'id': 'T1'}, 'user': {'id': 'U1'}}, repo)
    repo.slack_actor.assert_not_called()


def test_search_routes_with_prefix_and_checks_owned_review(monkeypatch):
    monkeypatch.setenv('EMAIL_REVIEW_ENABLED', 'true')
    repo = Mock()
    repo.masters.return_value = [{'id': 'company', 'name': '試験株式会社'}]
    monkeypatch.setattr(slack, 'Repository', lambda: repo)
    monkeypatch.setattr(slack, 'actor', lambda *args: 'member')
    payload = {'type': 'block_suggestion', 'action_id': 'crm_email_company_id', 'value': '試験', 'view': {'private_metadata': json.dumps({'id': 'review'})}}
    assert slack.handles(payload)
    result = json.loads(slack.handle(payload)['body'])
    assert result['options'][0]['value'] == 'company'
    repo.get.assert_called_once_with('review', 'member')
