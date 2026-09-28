from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock
from src.reports.durable.application import ReportRunner
from tests.reports.durable.test_domain import page


class Journal:
    def __init__(self, phase='project'):
        self.job = {'reportDate': date(2026,9,25), 'cutoff': datetime(2026,9,25,10,tzinfo=timezone.utc), 'phase':phase, 'cursor':{}}
        self.stored = {}; self.deliveries = {}; self.errors = []
    def save_page(self, job, pages, cursor, complete):
        for item in pages: self.stored[(job['phase'],item['id'])] = item
        self.job = {**job, 'cursor': {} if complete else cursor,
            'phase': ('action' if job['phase']=='project' else 'ready') if complete else job['phase']}
    def get(self, report_date): return self.job
    def delivery(self, report_date, kind): return self.deliveries.get(kind)
    def reserve(self, report_date, kind, destination_hash, body_hash): self.deliveries[kind] = {'state':'reserved','destinationHash':destination_hash}
    def delivered(self, report_date, kind): self.deliveries[kind]['state'] = 'delivered'
    def finish(self, report_date): self.job['phase'] = 'done'
    def error(self, report_date, reason, held=False):
        self.errors.append(reason)
        if held: self.job['phase'] = 'held'


def test_collection_resumes_and_only_sends_after_both_sources_complete():
    journal=Journal(); now=[0]
    def project(body):
        now[0] += 2
        return {'results':[page()], 'has_more':False, 'request_status':None}
    clients={'project':SimpleNamespace(query_raw=project), 'action':SimpleNamespace(query_raw=lambda body: {'results':[page('a')], 'has_more':False, 'request_status':None})}
    render=Mock(return_value='合成日報'); send=Mock()
    runner=ReportRunner(journal,clients,render,send,clock=lambda: now[0])
    assert runner.run(journal.job,'https://invalid.example',collection_budget=1)['state']=='collecting'
    send.assert_not_called(); assert len(journal.stored)==1
    assert runner.run(journal.job,'https://invalid.example')['state']=='done'
    assert len(journal.stored)==2 and send.call_count==2
    assert set(journal.deliveries)=={'daily','weekly'}


def test_send_timeout_is_reserved_and_never_repeated_on_resume():
    journal=Journal('ready'); send=Mock(side_effect=TimeoutError('secret-url'))
    runner=ReportRunner(journal,{},Mock(return_value='合成日報'),send)
    assert runner.run(journal.job,'destination')['state']=='retry_pending'
    assert journal.deliveries['daily']['state']=='reserved'
    assert runner.run(journal.job,'destination')['state']=='held'
    assert send.call_count==1 and 'secret-url' not in str(journal.errors)


def test_friday_weekly_failure_does_not_resend_daily():
    journal=Journal('ready'); send=Mock(side_effect=[None,TimeoutError()])
    runner=ReportRunner(journal,{},Mock(return_value='合成'),send)
    runner.run(journal.job,'destination'); runner.run(journal.job,'destination')
    assert send.call_count==2
    assert journal.deliveries['daily']['state']=='delivered'
    assert journal.deliveries['weekly']['state']=='reserved'


def test_missing_destination_is_not_reported_as_sent():
    journal=Journal('ready'); send=Mock()
    result=ReportRunner(journal,{},Mock(),send).run(journal.job,None)
    assert result['state']=='not_configured' and not result['daily_report_sent']
    send.assert_not_called()


def test_collection_timeout_records_stage_without_exception_body():
    import requests
    journal = Journal(); send = Mock()
    client = SimpleNamespace(query_raw=Mock(side_effect=requests.exceptions.Timeout('secret-url/private-body')))
    result = ReportRunner(journal, {'project': client}, Mock(), send).run(journal.job, 'destination')
    assert result['state'] == 'retry_pending'
    assert journal.errors[-1].startswith('Notion収集で時間切れ。')
    assert 'secret' not in journal.errors[-1] and 'private' not in journal.errors[-1]
    send.assert_not_called()


def test_page_save_failure_is_distinguished_from_notion_failure():
    journal = Journal(); send = Mock()
    journal.save_page = Mock(side_effect=RuntimeError('private page body'))
    client = SimpleNamespace(query_raw=lambda body: {'results': [page()], 'has_more': False})
    ReportRunner(journal, {'project': client}, Mock(), send).run(journal.job, 'destination')
    assert journal.errors[-1].startswith('収集ページの保存で処理エラー。')
    assert 'private' not in journal.errors[-1]
    send.assert_not_called()


def test_delivery_save_failure_keeps_reservation_and_does_not_resend():
    journal = Journal('ready'); send = Mock()
    journal.delivered = Mock(side_effect=RuntimeError('secret db connection'))
    runner = ReportRunner(journal, {}, Mock(return_value='合成日報'), send)
    runner.run(journal.job, 'destination')
    assert journal.errors[-1].startswith('送達結果の保存で処理エラー。')
    assert 'secret' not in journal.errors[-1]
    assert journal.deliveries['daily']['state'] == 'reserved'
    runner.run(journal.job, 'destination')
    assert send.call_count == 1


def test_failure_classification_uses_only_safe_codes():
    from src.reports.durable.application import safe_failure_reason
    from src.sync_engine.clients._http import ApiError
    import psycopg
    assert 'HTTP 429' in safe_failure_reason('Notion収集', ApiError(429, 'private url'))
    assert 'DB処理時間切れまたは取消' in safe_failure_reason('収集ページの保存', psycopg.errors.QueryCanceled('private query'))
    assert 'private' not in safe_failure_reason('収集ページの保存', psycopg.errors.QueryCanceled('private query'))


def test_oversized_report_is_held_before_delivery_reservation():
    journal = Journal('ready'); send = Mock()
    result = ReportRunner(journal, {}, Mock(return_value='あ' * 40001), send).run(journal.job, 'destination')
    assert result['state'] == 'held'
    assert journal.deliveries == {}
    assert '未送信' in journal.errors[-1]
    send.assert_not_called()


def test_oversized_weekly_report_preserves_delivered_daily_status():
    journal = Journal('ready'); send = Mock()
    render = Mock(side_effect=['日報', 'あ' * 40001])
    result = ReportRunner(journal, {}, render, send).run(journal.job, 'destination')
    assert result['state'] == 'held'
    assert journal.deliveries['daily']['state'] == 'delivered'
    assert 'weekly' not in journal.deliveries
    assert journal.errors[-1] == '週報本文がSlackの文字数上限を超えています（週報は未送信）'
    assert send.call_count == 1
