from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock
import pytest
from src.calendar_sync.tasks import ActionTask
from src.calendar_sync.todo import TodoSynchronizer, TODO_DATABASE_ID, payload
from src.calendar_sync.service import sync_project_actions
from tests.calendar_sync.test_tasks import PROJECT


class Journal:
    def __init__(self): self.rows = {}; self.holds = {}; self.calendar_rows = {}; self.hints = {}; self.batches = {}; self.versions = {}
    def lock(self, key): return nullcontext()
    def get(self, key): return deepcopy(self.rows.get(key))
    def reserve(self, key, value):
        if key in self.rows: return False
        self.rows[key] = {'state': 'reserved', 'metadata': {'baseline': value, 'pending': None}}
        return True
    def finish(self, key, page):
        self.rows[key].update(state='created', externalId=page)
        self.hold(key, '読戻し待ち')
    def metadata(self, key, baseline, pending=None):
        self.rows[key]['metadata'] = {'baseline': baseline, 'pending': pending}
        if pending: self.hold(key, '更新待ち')
    def hold(self, key, reason, *, page_id=None):
        self.holds[key] = reason
        if page_id: self.hints[key] = page_id
    def clear(self, key): self.holds.pop(key, None)
    def accept_source(self, key, source_at, source_hash):
        previous = self.versions.get(key)
        if previous and source_at < previous[0]: return 'stale'
        if previous and source_at == previous[0]: return 'accepted' if source_hash == previous[1] else 'conflict'
        self.versions[key] = (source_at, source_hash)
        self.batches.pop(key, None)
        return 'accepted'
    def batch_position(self, key, plan_hash):
        saved = self.batches.get(key, {})
        return saved.get('next_index', 0) if saved.get('plan_hash') == plan_hash else 0
    def batch_advance(self, key, plan_hash, next_index):
        self.batches[key] = {'plan_hash': plan_hash, 'next_index': next_index}
    def calendar_get(self, key): return deepcopy(self.calendar_rows.get(key))
    def calendar_reserve(self, key, request_hash):
        if self.calendar_rows.get(key, {}).get('state') == 'reserved': return False
        self.calendar_rows[key] = {'state': 'reserved'}
        return True
    def calendar_finish(self, key, *, event_id=None, skipped=None):
        self.calendar_rows[key] = {'state': 'blocked' if skipped else 'created', 'externalId': event_id}



class Client:
    def __init__(self): self.pages = {}; self.creates = 0; self.updates = 0
    def create(self, task):
        self.creates += 1
        page_id = f'page-{self.creates}'
        self.pages[page_id] = {'id': page_id, 'parent': {'database_id': TODO_DATABASE_ID},
                               'last_edited_time': 'v1', 'properties': payload(task, creating=True)}
        return page_id
    def read(self, page_id): return deepcopy(self.pages[page_id])
    def update(self, page_id, task):
        self.updates += 1
        self.pages[page_id]['properties'].update(payload(task))
        self.pages[page_id]['last_edited_time'] = f'v{self.updates + 1}'


def setup():
    client, journal = Client(), Journal()
    task = ActionTask('id-one', '電話', '2026-11-26', PROJECT, '案件', '次回アクション', ('user',))
    return TodoSynchronizer(client, journal), client, journal, task


def test_案件紐付け付きTODOを作成し再実行でも増やさない():
    service, client, journal, task = setup()
    assert service.sync(task)['status'] == 'verified'
    assert service.sync(task)['status'] == 'verified'
    assert client.creates == 1
    assert client.pages['page-1']['properties']['🤝 案件管理']['relation'] == [{'id': PROJECT}]
    assert not journal.holds


def test_作成応答不明では再POSTしない(monkeypatch):
    service, client, journal, task = setup()
    create = client.create
    def timeout(task): create(task); raise TimeoutError()
    monkeypatch.setattr(client, 'create', timeout)
    assert service.sync(task)['status'] == 'held'
    assert service.sync(task)['status'] == 'held'
    assert client.creates == 1 and journal.holds


def test_台帳ID保存障害でも再作成せず結果不明予約を残す(monkeypatch):
    service, client, journal, task = setup()
    monkeypatch.setattr(journal, 'finish', Mock(side_effect=RuntimeError()))
    assert service.sync(task)['status'] == 'held'
    assert service.sync(task)['status'] == 'held'
    assert client.creates == 1
    assert next(iter(journal.hints.values())) == "page-1"


def test_期限変更は同じTODOを更新():
    service, client, journal, task = setup()
    service.sync(task)
    assert service.sync(replace(task, due='2026-12-02'))['status'] == 'verified'
    assert client.creates == 1 and client.updates == 1
    assert client.pages['page-1']['properties']['期限']['date']['start'] == '2026-12-02'


@pytest.mark.parametrize('field,value', [('名前', {'title': [{'text': {'content': '人が変更'}}]}),
                                         ('ステータス', {'status': {'name': '完了'}}),
                                         ('期限', {'date': {'start': '2026-12-31'}})])
def test_人編集または完了は上書きせず予定も保留(field, value):
    service, client, journal, task = setup()
    service.sync(task)
    client.pages['page-1']['properties'][field] = value
    assert service.sync(replace(task, due='2026-12-02'))['status'] == 'held'
    assert client.updates == 0


def test_更新応答消失は読戻しが一致すれば再PATCHせず復旧(monkeypatch):
    service, client, journal, task = setup()
    service.sync(task)
    update = client.update
    def timeout(page_id, task): update(page_id, task); raise TimeoutError()
    monkeypatch.setattr(client, 'update', timeout)
    changed = replace(task, due='2026-12-02')
    assert service.sync(changed)['status'] == 'held'
    assert service.sync(changed)['status'] == 'verified'
    assert client.updates == 1


def test_Calendar設定なしでも期限なしTODOを作る():
    service, client, journal, task = setup()
    result = sync_project_actions({'__source_last_edited_time': '2026-11-25T00:00:00Z', '次回アクション': '- 電話\n- 提案 2026-12-02'}, PROJECT, todo_sync=service)
    assert client.creates == 2
    assert [r['calendar']['reason'] for r in result['tasks']] == ['task_date_missing', 'assignee_missing']


def test_タスクごと予定を作りTODO人編集時は予定を更新しない(monkeypatch):
    service, client, journal, task = setup()
    calendar = Mock()
    calendar.upsert_event.return_value = {'google_event_id': 'event', 'created': True}
    monkeypatch.setattr('src.calendar_sync.service.get_notion_user_email', lambda _: 'rep@example.com')
    props = {'__source_last_edited_time': '2026-11-25T00:00:00Z', '次回アクション': '- 電話 2026-11-26\n- 提案 2026-11-27', '担当メンバー': ['user']}
    sync_project_actions(props, PROJECT, todo_sync=service, calendar_client=calendar)
    assert calendar.upsert_event.call_count == 2
    assert len({c.kwargs['task_id'] for c in calendar.upsert_event.call_args_list}) == 2
    client.pages['page-1']['properties']['ステータス'] = {'status': {'name': '完了'}}
    calendar.reset_mock()
    result = sync_project_actions(props, PROJECT, todo_sync=service, calendar_client=calendar)
    assert calendar.upsert_event.call_count == 1
    assert result['tasks'][0]['calendar']['reason'] == 'todo_not_verified'


def test_旧予定の移行保留でTODOは失わない(monkeypatch):
    service, client, journal, task = setup()
    calendar = Mock()
    calendar.upsert_event.return_value = {'skipped': 'legacy_event_migration_required'}
    monkeypatch.setattr('src.calendar_sync.service.get_notion_user_email', lambda _: 'rep@example.com')
    props = {'__source_last_edited_time': '2026-11-25T00:00:00Z', '次回アクション': '電話', 'AI推奨アクション日': '2026-11-26', '担当メンバー': ['user']}
    result = sync_project_actions(props, PROJECT, todo_sync=service, calendar_client=calendar)
    assert result['tasks'][0]['todo']['status'] == 'verified'
    assert client.creates == 1 and journal.holds


def test_有効化時Calendar設定なしでもTODO配線を残す(monkeypatch):
    from src.sync_engine.production_wiring import build_calendar_sync_callable
    monkeypatch.setenv('PROJECT_ACTION_TODO_ENABLED', 'true')
    monkeypatch.setenv('NOTION_API_KEY', 'test-notion')
    monkeypatch.delenv('CALENDAR_SYNC_API_TOKEN', raising=False)
    monkeypatch.delenv('WEB_ENGAGEMENT_TOOL_URL', raising=False)
    wired = build_calendar_sync_callable()
    assert wired is not None
    assert isinstance(wired.keywords['todo_sync'], TodoSynchronizer)
    assert wired.keywords['calendar_client'] is None


def test_HTTP作成要求は単発で実DBの項目型を使う(monkeypatch):
    from src.calendar_sync.todo import NotionTodoClient
    service, client, journal, task = setup()
    response = Mock(status_code=200)
    response.json.return_value = {'id': PROJECT}
    request = Mock(return_value=response)
    monkeypatch.setattr('src.calendar_sync.todo.request_with_retry', request)
    assert NotionTodoClient(api_key='test').create(task) == PROJECT
    args, kwargs = request.call_args
    assert args[0] == 'POST'
    assert kwargs['max_retries'] == 0 and kwargs['max_rate_limit_retries'] == 0
    assert kwargs['idempotent'] is False
    props = kwargs['json_body']['properties']
    assert props['ステータス'] == {'status': {'name': '未着手'}}
    assert props['🤝 案件管理']['relation'] == [{'id': PROJECT}]
    assert '担当者2' not in props


def test_TODOから予定完了まで同じタスクを直列化する(monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    service, client, journal, task = setup()
    mutex, first_calendar, second_waiting, release = threading.Lock(), threading.Event(), threading.Event(), threading.Event()
    calls = []
    def lock(key):
        if key.startswith('calendar-task-plan:'): return nullcontext()
        if first_calendar.is_set(): second_waiting.set()
        return mutex
    monkeypatch.setattr(journal, 'lock', lock)
    monkeypatch.setattr('src.calendar_sync.service.get_notion_user_email', lambda _: 'rep@example.com')
    class Calendar:
        def upsert_event(self, **kwargs):
            if kwargs['date'] == '2026-11-26':
                first_calendar.set()
                assert release.wait(5)
            calls.append(kwargs['date'])
            return {'google_event_id': 'event', 'created': False}
    props = {'__source_last_edited_time': '2026-11-25T00:00:00Z', '次回アクション': '電話', 'AI推奨アクション日': '2026-11-26', '担当メンバー': ['user']}
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(sync_project_actions, props, PROJECT, todo_sync=service, calendar_client=Calendar())
        assert first_calendar.wait(5)
        new = pool.submit(sync_project_actions, {**props, '__source_last_edited_time': '2026-11-25T01:00:00Z', 'AI推奨アクション日': '2026-12-02'}, PROJECT,
                          todo_sync=service, calendar_client=Calendar())
        try:
            assert second_waiting.wait(5)
            assert client.updates == 0
        finally:
            release.set()
        old.result(); new.result()
    assert calls == ['2026-11-26', '2026-12-02']
    assert client.pages['page-1']['properties']['期限']['date']['start'] == '2026-12-02'
    assert not journal.holds


def test_Calendar応答不明は遅延旧更新の可能性があるため次のTODO更新も止める(monkeypatch):
    service, client, journal, task = setup()
    calendar = Mock()
    calendar.upsert_event.side_effect = TimeoutError('相手側では処理中')
    monkeypatch.setattr('src.calendar_sync.service.get_notion_user_email', lambda _: 'rep@example.com')
    props = {'__source_last_edited_time': '2026-11-25T00:00:00Z', '次回アクション': '電話', 'AI推奨アクション日': '2026-11-26', '担当メンバー': ['user']}
    first = sync_project_actions(props, PROJECT, todo_sync=service, calendar_client=calendar)
    second = sync_project_actions({**props, '__source_last_edited_time': '2026-11-25T01:00:00Z', 'AI推奨アクション日': '2026-12-02'}, PROJECT,
                                  todo_sync=service, calendar_client=calendar)
    assert first['tasks'][0]['calendar']['reason'] == 'calendar_result_unknown'
    assert second['tasks'][0]['todo']['reason'] == 'calendar_result_unknown'
    assert calendar.upsert_event.call_count == 1 and client.updates == 0
    assert client.pages['page-1']['properties']['期限']['date']['start'] == '2026-11-26'
    assert journal.holds


def test_時間不足ではTODO予約もPOSTも開始しない(monkeypatch):
    service, client, journal, task = setup()
    monkeypatch.setattr('src.calendar_sync.service.remaining', lambda: 1)
    result = sync_project_actions({'__source_last_edited_time': '2026-11-25T00:00:00Z', '次回アクション': '電話', 'AI推奨アクション日': '2026-11-26'},
                                  PROJECT, todo_sync=service)
    assert result['tasks'][0]['todo']['reason'] == 'time_budget'
    assert client.creates == 0 and not journal.rows
    assert PROJECT.replace('-', '') in next(iter(journal.holds.values()))


def test_時間延期後は続きから再開して末尾まで到達する(monkeypatch):
    service, client, journal, task = setup()
    props = {'__source_last_edited_time': '2026-11-25T00:00:00Z', '次回アクション': '- 電話\n- 提案\n- 共有'}
    budget = iter([True, False])
    monkeypatch.setattr('src.calendar_sync.service._has_task_budget', lambda _: next(budget))
    first = sync_project_actions(props, PROJECT, todo_sync=service)
    assert client.creates == 1 and first['remaining_count'] == 2
    budget = iter([True, False])
    second = sync_project_actions(props, PROJECT, todo_sync=service)
    assert second['resume_from'] == 1 and client.creates == 2
    budget = iter([True])
    third = sync_project_actions(props, PROJECT, todo_sync=service)
    assert third['resume_from'] == 2 and third['remaining_count'] == 0
    assert client.creates == 3


def test_新経路だけCalendar読戻しの待ち時間を確保する(monkeypatch):
    from src.sync_engine.production_wiring import build_calendar_sync_callable
    monkeypatch.setenv('PROJECT_ACTION_TODO_ENABLED', 'true')
    monkeypatch.setenv('NOTION_API_KEY', 'test-notion')
    monkeypatch.setenv('CALENDAR_SYNC_API_TOKEN', 'test-calendar')
    monkeypatch.setenv('WEB_ENGAGEMENT_TOOL_URL', 'https://example.com')
    wired = build_calendar_sync_callable()
    assert wired.keywords['calendar_client']._timeout == 10


def test_新原本の同期完了後に古い取得済み原本を投入しても巻き戻さない(monkeypatch):
    service, client, journal, task = setup()
    calendar = Mock()
    calendar.upsert_event.return_value = {'google_event_id': 'event', 'created': False}
    monkeypatch.setattr('src.calendar_sync.service.get_notion_user_email', lambda _: 'rep@example.com')
    latest = {'__source_last_edited_time': '2026-11-25T02:00:00Z', '次回アクション': '電話',
              'AI推奨アクション日': '2026-12-02', '担当メンバー': ['user']}
    sync_project_actions(latest, PROJECT, todo_sync=service, calendar_client=calendar)
    old = {**latest, '__source_last_edited_time': '2026-11-25T01:00:00Z', 'AI推奨アクション日': '2026-11-26'}
    result = sync_project_actions(old, PROJECT, todo_sync=service, calendar_client=calendar)
    assert result['skipped'] == 'stale_source_version'
    assert client.pages['page-1']['properties']['期限']['date']['start'] == '2026-12-02'
    assert calendar.upsert_event.call_count == 1 and client.updates == 0
    assert not journal.holds


def test_原本時刻不明または同時刻別内容は書込前に保留():
    service, client, journal, task = setup()
    props = {'次回アクション': '電話', 'AI推奨アクション日': '2026-11-26'}
    assert sync_project_actions(props, PROJECT, todo_sync=service)['reason'] == 'source_version_missing'
    assert client.creates == 0 and not journal.versions
    props['__source_last_edited_time'] = '2026-11-25T01:00:00Z'
    sync_project_actions(props, PROJECT, todo_sync=service)
    changed = {**props, 'AI推奨アクション日': '2026-12-02'}
    assert sync_project_actions(changed, PROJECT, todo_sync=service)['reason'] == 'source_version_conflict'
    assert client.creates == 1 and client.updates == 0


def test_最初の外部書込より前に原本時刻を保存する(monkeypatch):
    service, client, journal, task = setup()
    create = client.create
    def checked(task):
        assert journal.versions[f'calendar-task-plan:{PROJECT}'][0] == '2026-11-25T01:00:00+00:00'
        return create(task)
    monkeypatch.setattr(client, 'create', checked)
    props = {'__source_last_edited_time': '2026-11-25T01:00:00Z', '次回アクション': '電話'}
    sync_project_actions(props, PROJECT, todo_sync=service)
    assert client.creates == 1
