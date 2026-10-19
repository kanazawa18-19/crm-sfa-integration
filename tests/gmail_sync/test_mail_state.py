"""下書きの誤登録と旧受付の再配送を、送信証拠の有無で防ぐ。"""
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from src.gmail_sync import gmail_client, sync
from src.gmail_sync.mail_state import eligible, MailStateUnknown
from src.email_review import domain
from src.email_review.service import Service, capture
from tests.email_review.test_service import Repo, Gateway


def message(labels=('SENT',), sender='rep@example.test'):
    return gmail_client.GmailMessage('m', sender, 'buyer@outside.test', '合成', None, '要約',
        internal_date_ms='1780000000000', body_text='本文', label_ids=list(labels) if labels is not None else None)


@pytest.mark.parametrize('labels,expected', [(['DRAFT'],False),(['DRAFT','SENT'],False),(['SENT'],True),([],False),(['INBOX'],False)])
def test_送信者一致だけでは送信メールにしない(labels, expected):
    assert eligible(message(labels), 'rep@example.test') is expected


def test_受信の署名照合は継続しラベル欠損は再取得():
    assert eligible(message(['INBOX'], 'buyer@outside.test'), 'rep@example.test')
    with pytest.raises(MailStateUnknown): eligible(message(None), 'rep@example.test')


def test_下書きは照合保存も履歴保存も行わない(monkeypatch):
    monkeypatch.setenv('EMAIL_REVIEW_ENABLED','true')
    repo = Mock()
    capture(message(['DRAFT']), 'rep@example.test', repo=repo)
    repo.capture.assert_not_called()
    save = Mock(side_effect=AssertionError('保存禁止'))
    monkeypatch.setattr(sync,'_record_message', save)
    assert sync.record_message(message(['DRAFT']), 'rep@example.test', object(), internal_domains=set()) is False
    save.assert_not_called()


@pytest.mark.parametrize('method', ['evaluate','process','reconcile'])
@pytest.mark.parametrize('labels', [None, ['DRAFT'], ['DRAFT','SENT'], []])
@pytest.mark.parametrize('created', [False, True])
def test_保存済みの予約も外部操作前に止める(method, labels, created):
    gateway = Mock()
    row = {'id':'r', 'revision':0, 'direction':'outbound', 'state':'approved',
           'message':{'label_ids':labels}, **({'action_id':'action'} if created else {})}
    result = getattr(Service(Repo(),gateway),method)(row)
    assert result['state'] == ('held' if created or labels is None else 'excluded')
    assert not gateway.mock_calls


@pytest.mark.parametrize('action', ['confirm','resume','reconcile'])
def test_画面の承認でも送信不明を迂回しない(action):
    with pytest.raises(domain.Conflict, match='送信済み'):
        domain.decide({'revision':0,'state':'unknown' if action=='reconcile' else 'pending',
                       'direction':'outbound','message':{}}, revision=0, action=action,
                      selection={}, actor='actor', channel='dashboard')


def test_実読SENTだけ通常の作成へ進む():
    gateway = Gateway()
    result = Service(Repo(),gateway).process({'id':'r','revision':0,'direction':'outbound',
        'message':{'label_ids':['SENT']}, 'owner':'rep@example.test','contact_id':'c','company_id':'company'})
    assert result['delivery']=='delivered' and gateway.created==1


def test_GAS下書きは正常な対象外で後続を止めない(monkeypatch):
    from src.sync_engine.webhook_handlers import gmail_ingest_webhook as ingest
    monkeypatch.setattr(ingest,'_process_logged_message',Mock(side_effect=AssertionError('保存禁止')))
    assert ingest._process_one(message(['DRAFT']),'rep@example.test',None,None,False)=='excluded'
    assert ingest._process_one(message(['DRAFT']),'rep@example.test',None,None,True)=='excluded'
    with pytest.raises(MailStateUnknown): ingest._process_one(message(None),'rep@example.test',None,None,False)


def test_通常通知は作成と既存配送の両方を止める(monkeypatch):
    from src.sync_engine.production_wiring import SkipTrackingDispatcher
    from src.sync_engine.sync_event import SyncEvent
    from src.db_schema.base import Tool
    from src.hub_creation.service import HubCreationService
    monkeypatch.setattr('src.email_review.repository.action_registration_blocked',lambda _:True)
    event=SyncEvent(Tool.NOTION,'action','page',datetime.now(timezone.utc))
    underlying, creation=Mock(),Mock()
    result=SkipTrackingDispatcher(underlying,creation_service=creation).dispatch(event)
    assert result.skipped and result.reason=='email_sent_evidence_required'
    assert not underlying.mock_calls and not creation.mock_calls
    service=HubCreationService(store=Mock(),journal=Mock(),notion_clients={},adapters=[],enabled_since=datetime.now(timezone.utc))
    assert service.handle(event)=='email_sent_evidence_required'


def test_取得APIの実ラベルを保存する(requests_mock, monkeypatch):
    monkeypatch.delenv('EMAIL_REVIEW_ENABLED',raising=False)
    requests_mock.get('https://gmail.googleapis.com/gmail/v1/users/me/messages/m',json={'id':'m','labelIds':['DRAFT']})
    assert gmail_client.get_message('fake','m').label_ids==['DRAFT']
    assert gmail_client.get_message_labels('fake','m')==['DRAFT']


@pytest.mark.parametrize('fields', [{}, {'labelIds':[]}])
def test_実APIのラベル省略は空配列で受信アーカイブを止めない(fields, requests_mock, monkeypatch):
    monkeypatch.delenv('EMAIL_REVIEW_ENABLED',raising=False)
    requests_mock.get('https://gmail.googleapis.com/gmail/v1/users/me/messages/m',json={
        'id':'m','payload':{'headers':[{'name':'From','value':'buyer@outside.test'}]},**fields})
    item=gmail_client.get_message('fake','m')
    assert item.label_ids==[] and eligible(item,'rep@example.test')
    assert gmail_client.get_message_labels('fake','m')==[]
    with pytest.raises(MailStateUnknown): eligible(message(None),'rep@example.test')


def test_SENT追加を拾い再出現IDの最後の履歴順を維持(requests_mock):
    requests_mock.get('https://gmail.googleapis.com/gmail/v1/users/me/history',json={'history':[
        {'id':'1','messagesAdded':[{'message':{'id':'m'}}]},
        {'id':'2','messagesAdded':[{'message':{'id':'other'}}]},
        {'id':'3','labelsAdded':[{'message':{'id':'m'},'labelIds':['SENT']}]},
        {'id':'4','labelsAdded':[{'message':{'id':'ignored'},'labelIds':['STARRED']}]},
    ],'historyId':'4'})
    result=gmail_client.list_history('fake','0')
    assert result.message_ids==['other','m']
    assert result.message_history_ids=={'m':'3','other':'2'}
    assert requests_mock.last_request.qs['historytypes']==['messageadded','labeladded']
