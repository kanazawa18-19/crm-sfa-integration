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


@pytest.mark.parametrize('verified', [False, None, True])
def test_送信証拠なしは理由と再取得案内だけを表示する(verified):
    row={'id':'r','revision':0,'subject':'合成','participants':[],'reason':'確認中',
         'state':'held','delivery':'not_started','direction':'outbound','sent_evidence':verified}
    view=slack.detail(row)
    if verified:
        assert view['submit']['text']=='受け付ける'
    else:
        assert 'submit' not in view
        assert not any(block['type']=='input' for block in view['blocks'])
        assert '再取得' in json.dumps(view,ensure_ascii=False)


@pytest.mark.parametrize('state', ['held', 'unknown', 'excluded'])
def test_訂正予約は操作を隠し取消済みでは理由を優先する(state):
    row={'id':'r','revision':0,'subject':'合成','participants':[],
         'reason':'下書き誤登録を取消しました。配送状態は取消前の履歴です' if state=='excluded' else '固定訂正予約',
         'state':state,'delivery':'partial','direction':'outbound','sent_evidence':True,'correction_active':True}
    view=slack.detail(row)
    text=json.dumps(view,ensure_ascii=False)
    assert 'submit' not in view
    assert not any(block['type']=='input' for block in view['blocks'])
    if state=='excluded':
        assert '取消しました' in text and '訂正処理中' not in text and '再開を依頼' not in text
    else:
        assert '訂正処理中' in text and '専用手順での再開' in text
