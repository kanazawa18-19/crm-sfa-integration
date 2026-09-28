"""既存の日報集計へ、収集完了した同一日付の保存データだけを渡す。"""
import os
import re
from datetime import datetime, timedelta, timezone
import requests
from src.api.dashboard_service import NotionDataSource, PROP_担当営業
from src.api.user_directory import NotionUserDirectory
from src.api.notion_display import page_to_display_dict, project_page_to_mirror_record
from src.db_schema.registry import get_schema
from src.reports.batch import run_daily_report, run_weekly_report
from src.reports.durable.application import ReportRunner
from src.reports.durable.journal import ReportJournal
from src.sync_engine.clients.notion_client import HttpNotionClient
from src.sync_engine.record_sync_lock import acquire_record_sync_lock, RecordSyncBusy


class CollectedSource(NotionDataSource):
    def __init__(self, journal, report_date):
        self.journal, self.report_date = journal, report_date
        self._user_directory = NotionUserDirectory(timeout=10, max_retries=0, max_rate_limit_retries=0)

    def get_projects(self):
        return [project_page_to_mirror_record(page, self._user_directory)[0]
                for page in self.journal.pages(self.report_date, 'project')]

    def get_actions(self):
        records = [page_to_display_dict(page, get_schema('action'))[0]
                   for page in self.journal.pages(self.report_date, 'action')]
        for record in records:
            record[PROP_担当営業] = self._resolve_assignee(record.get(PROP_担当営業))
        return records


class Capture:
    def send_report(self, text):
        pass


def render(report_date, kind, journal):
    source = CollectedSource(journal, report_date)
    function = run_daily_report if kind == 'daily' else run_weekly_report
    return function(report_date, data_source=source, notifier=Capture())


def report_destination():
    """本人DMが明示設定されている場合だけ配送する。"""
    channel = os.environ.get('SLACK_REPORT_CHANNEL_ID')
    if channel is not None:
        if not re.fullmatch(r'D[A-Z0-9]+', channel):
            raise ValueError('日報の本人DM設定が不正です')
        if not os.environ.get('SLACK_BOT_TOKEN'):
            raise ValueError('日報のSlack認証が未設定です')
        return 'slack-dm:' + channel
    return None


def send(destination, text):
    if len(text) > 40000:
        raise ValueError('日報本文がSlackの文字数上限を超えています')
    if destination.startswith('slack-dm:'):
        channel = destination.removeprefix('slack-dm:')
        if not re.fullmatch(r'D[A-Z0-9]+', channel):
            raise ValueError('日報の本人DM設定が不正です')
        response = requests.post('https://slack.com/api/chat.postMessage',
            headers={'Authorization': 'Bearer ' + os.environ['SLACK_BOT_TOKEN']},
            json={'channel': channel, 'text': text, 'unfurl_links': False, 'unfurl_media': False}, timeout=10)
        if not 200 <= response.status_code < 300:
            raise RuntimeError('日報の送達受付を確認できません')
        result = response.json()
        if result.get('ok') is not True or result.get('channel') != channel or not result.get('ts'):
            raise RuntimeError('日報の送達受付を確認できません')
        return
    response = requests.post(destination, json={'text': text}, timeout=10)
    if not 200 <= response.status_code < 300:
        # 応答のURL/本文を例外へ混ぜない。予約を残して実物確認へ回す。
        raise RuntimeError('日報の送達受付を確認できません')


def run_durable_report_batch(*, start_today=True):
    journal = ReportJournal()
    now = datetime.now(timezone.utc)
    local = now.astimezone(timezone(timedelta(hours=9)))
    try:
        with acquire_record_sync_lock(None, 'report', 'daily-weekly-batch'):
            journal.detect_missing_dates(local.date())
            if start_today and local.hour >= 19:
                journal.ensure(local.date(), now)
            job = journal.next_job()
            if job is None: return {'state': 'idle', 'daily_report_sent': False}
            clients = {key: HttpNotionClient(key, get_schema(key).notion_database_id,
                timeout=10, max_retries=0, max_rate_limit_retries=0) for key in ('project', 'action')}
            return ReportRunner(journal, clients, render, send).run(job, report_destination())
    except RecordSyncBusy:
        return {'state': 'busy', 'daily_report_sent': False}
