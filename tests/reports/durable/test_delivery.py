from unittest.mock import Mock
import pytest
from src.reports.durable.production import report_destination,send

@pytest.fixture
def dm(monkeypatch):
    monkeypatch.setenv('SLACK_REPORT_CHANNEL_ID','DTEST123')
    monkeypatch.setenv('SLACK_BOT_TOKEN','synthetic-token')
    monkeypatch.setenv('SLACK_WEBHOOK_URL_REPORT','https://invalid.example/shared')


def test_dm_overrides_old_shared_destination(dm,monkeypatch):
    post=Mock(return_value=Mock(status_code=200,json=lambda:{'ok':True,'channel':'DTEST123','ts':'1.2'}))
    monkeypatch.setattr('src.reports.durable.production.requests.post',post)
    assert report_destination()=='slack-dm:DTEST123'
    send(report_destination(),'合成日報')
    assert post.call_count==1
    assert post.call_args.args==('https://slack.com/api/chat.postMessage',)
    assert post.call_args.kwargs['json']['channel']=='DTEST123'


@pytest.mark.parametrize('value',['','CTEST123','DTEST/invalid'])
def test_invalid_dm_never_falls_back_to_shared_webhook(dm,monkeypatch,value):
    monkeypatch.setenv('SLACK_REPORT_CHANNEL_ID',value)
    with pytest.raises(ValueError):report_destination()


def test_missing_dm_never_uses_old_shared_webhook(dm,monkeypatch):
    monkeypatch.delenv('SLACK_REPORT_CHANNEL_ID')
    assert report_destination() is None


def test_oversized_report_stops_before_sending(dm,monkeypatch):
    post=Mock()
    monkeypatch.setattr('src.reports.durable.production.requests.post',post)
    with pytest.raises(ValueError,match='文字数上限'):
        send(report_destination(),'あ'*40001)
    post.assert_not_called()


@pytest.mark.parametrize('result',[{'ok':False,'error':'private'}, {'ok':True,'channel':'DOTHER','ts':'1.2'}, {'ok':True,'channel':'DTEST123'}])
def test_rejected_or_mismatched_receipt_is_not_delivered(dm,monkeypatch,result):
    monkeypatch.setattr('src.reports.durable.production.requests.post',Mock(return_value=Mock(status_code=200,json=lambda:result)))
    with pytest.raises(RuntimeError,match='送達受付') as exc:send(report_destination(),'合成日報')
    assert 'private' not in str(exc.value)
