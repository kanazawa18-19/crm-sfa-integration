"""案件TODOの保存・読戻し。人が変えた管理項目や完了状態を自動上書きしない。"""
from __future__ import annotations
import hashlib
import json
import logging
import os
from urllib.parse import quote
from uuid import UUID
from src.calendar_sync.tasks import ActionTask
from src.calendar_sync.todo_journal import TodoJournal
from src.sync_engine.clients._http import request_with_retry, raise_for_error
from src.sync_engine.clients.notion_client import NotionApiError

TODO_DATABASE_ID = '86f4b46c-714b-48e4-9327-62f7ef73887f'
PROJECT_RELATION = '🤝 案件管理'
logger = logging.getLogger(__name__)


def desired_snapshot(task: ActionTask):
    return {'name': task.title, 'due': {'start': task.due, 'end': None, 'time_zone': None} if task.due else None,
            'assignees': sorted(task.assignees), 'projects': [task.project_id.replace('-', '')],
            'status': '未着手'}


def snapshot(page):
    props = page.get('properties') or {}
    title = props.get('名前', {}).get('title', [])
    due = props.get('期限', {}).get('date')
    return {'name': ''.join(p.get('plain_text', p.get('text', {}).get('content', '')) for p in title),
            'due': {key: due.get(key) for key in ('start', 'end', 'time_zone')} if due else None,
            'assignees': sorted(p['id'] for p in props.get('担当者', {}).get('people', [])),
            'projects': sorted(p['id'].replace('-', '') for p in props.get(PROJECT_RELATION, {}).get('relation', [])),
            'status': (props.get('ステータス', {}).get('status') or {}).get('name')}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def payload(task, *, creating=False):
    result = {'名前': {'title': [{'text': {'content': task.title}}]},
              '期限': {'date': {'start': task.due} if task.due else None},
              '担当者': {'people': [{'id': user} for user in task.assignees]},
              PROJECT_RELATION: {'relation': [{'id': task.project_id}]}}
    if creating:
        result['ステータス'] = {'status': {'name': '未着手'}}
    return result


class NotionTodoClient:
    def __init__(self, *, api_key=None):
        self.api_key = api_key or os.environ.get('NOTION_API_KEY')
        if not self.api_key:
            raise ValueError('Notion TODO用の認証が未設定です')

    def _request(self, method, path, body=None):
        response = request_with_retry(method, 'https://api.notion.com/v1' + path,
            headers={'Authorization': 'Bearer ' + self.api_key, 'Notion-Version': '2022-06-28'},
            json_body=body, timeout=10, max_retries=1 if method == 'GET' else 0,
            max_rate_limit_retries=1 if method == 'GET' else 0, idempotent=method == 'GET')
        raise_for_error(response, NotionApiError)
        return response.json()

    def create(self, task):
        page = self._request('POST', '/pages', {'parent': {'database_id': TODO_DATABASE_ID},
                                               'properties': payload(task, creating=True)})
        return str(UUID(page['id']))

    def read(self, page_id):
        return self._request('GET', '/pages/' + quote(page_id, safe=''))

    def update(self, page_id, task):
        self._request('PATCH', '/pages/' + quote(page_id, safe=''), {'properties': payload(task)})


class TodoSynchronizer:
    def __init__(self, client=None, journal=None):
        self.client = client if client is not None else NotionTodoClient()
        self.journal = journal if journal is not None else TodoJournal()

    def sync(self, task: ActionTask):
        key = f'calendar-task:{task.project_id}:{task.key}'
        with self.journal.lock(key):
            return self.sync_locked(task)

    def sync_locked(self, task: ActionTask):
        """呼出元がTODOから予定の応答確認まで同じ鍵を保持する場合の入口。"""
        key = f'calendar-task:{task.project_id}:{task.key}'
        try:
            return self._sync(task, key)
        except Exception:
            self.journal.hold(key, 'TODO保存・読戻しに失敗しました。予約と作成済みIDを確認してください')
            return {'status': 'held', 'reason': 'todo_io_failed'}

    def _held(self, key, reason, page_id=None):
        self.journal.hold(key, reason, page_id=page_id)
        return {'status': 'held', 'reason': reason, 'page_id': page_id}

    def _sync(self, task, key):
        desired = desired_snapshot(task)
        expected = digest(desired)
        row = self.journal.get(key)
        if row is None:
            if not self.journal.reserve(key, expected):
                return self._held(key, 'TODOの作成予約が進行中です')
            # 非冪等POSTは一度だけ。ID確定前の中断はreservedのまま手動確認する。
            page_id = self.client.create(task)
            try:
                self.journal.finish(key, page_id)
            except Exception:
                logger.error('TODO作成IDの台帳保存失敗: key=%s page_id=%s', key, page_id)
                return self._held(key, f'TODO作成済みID {page_id} の台帳保存を確認してください。再作成は停止中です', page_id)
            row = self.journal.get(key)
        if row is None or row['state'] != 'created' or not row.get('externalId'):
            return self._held(key, 'TODOの作成結果が不明です。自動再作成せず確認を待っています')
        page_id = row['externalId']
        page = self.client.read(page_id)
        if (page.get('archived') or page.get('in_trash') or
                page.get('parent', {}).get('database_id', '').replace('-', '') != TODO_DATABASE_ID.replace('-', '')):
            return self._held(key, 'TODOが無効または別DBに移動しています', page_id)
        current = snapshot(page)
        if current['status'] != '未着手':
            return self._held(key, 'TODOのステータスが変更済みのため、TODOと予定を保留しました', page_id)
        actual = digest(current)
        metadata = row['metadata']
        if metadata.get('pending'):
            if actual != metadata['pending']:
                return self._held(key, 'TODO更新の結果が未確定です。再更新せず確認を待っています', page_id)
            self.journal.metadata(key, actual)
            metadata = {'baseline': actual}
        if actual != metadata['baseline']:
            return self._held(key, 'TODOが人に編集されています。TODOと予定を自動上書きしません', page_id)
        if actual != expected:
            # Notionには条件付きPATCHがないため、書込直前にも読み直して変更を検出する。
            latest = self.client.read(page_id)
            if latest.get('last_edited_time') != page.get('last_edited_time') or digest(snapshot(latest)) != actual:
                return self._held(key, 'TODOが照合中に変更されました', page_id)
            self.journal.metadata(key, actual, expected)
            self.client.update(page_id, task)
            verified = self.client.read(page_id)
            if verified.get('archived') or verified.get('in_trash') or digest(snapshot(verified)) != expected:
                return self._held(key, 'TODO更新後の保存値を確認できません', page_id)
            self.journal.metadata(key, expected)
        self.journal.clear(key)
        return {'status': 'verified', 'page_id': page_id}
