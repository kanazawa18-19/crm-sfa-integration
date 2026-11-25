"""既存予約表の専用targetで、TODOの作成・更新と確認待ちを永続化する。"""
from concurrent.futures import ThreadPoolExecutor
import threading
import psycopg
import pytest
from tests.hub_creation.test_scan_postgres import local
from src.hub_creation.journal import PostgresCreationJournal, connect
from src.calendar_sync.todo_journal import TodoJournal


def test_同じTODOの並列予約は一つだけ(local):
    journal = TodoJournal()
    barrier = threading.Barrier(2)
    def reserve(_):
        barrier.wait()
        return journal.reserve('calendar-task:project:one', 'digest')
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(reserve, range(2))) == [False, True]
    assert TodoJournal().get('calendar-task:project:one')['state'] == 'reserved'


def test_作成IDと読戻し確認待ちが同時に保存される(local):
    journal = TodoJournal()
    key = 'calendar-task:project:one'
    journal.reserve(key, 'baseline')
    journal.finish(key, 'page')
    row = TodoJournal().get(key)
    assert row['externalId'] == 'page' and row['metadata']['baseline'] == 'baseline'
    assert PostgresCreationJournal().get(key, 'notion_todo_review')['state'] == 'blocked'
    journal.clear(key)
    journal.metadata(key, 'baseline', 'updated')
    assert journal.get(key)['metadata']['pending'] == 'updated'
    assert PostgresCreationJournal().get(key, 'notion_todo_review')['state'] == 'blocked'


def test_確認待ち保存失敗ならID確定もロールバックする(local):
    journal = TodoJournal()
    key = 'calendar-task:project:one'
    journal.reserve(key, 'baseline')
    with connect() as conn:
        conn.execute('''ALTER TABLE "HubCreationAttempt" ADD CONSTRAINT reject_todo_review
                       CHECK (target <> 'notion_todo_review')''')
    with pytest.raises(psycopg.errors.CheckViolation):
        journal.finish(key, 'page')
    assert journal.get(key)['state'] == 'reserved'
    assert journal.get(key)['externalId'] is None
    assert not journal.reserve(key, 'baseline')


def test_既存Hub登録台帳の値を変更しない(local):
    existing = PostgresCreationJournal()
    existing.reserve('calendar-task:project:one', 'zoho', 'chain', 'other')
    existing.finish('calendar-task:project:one', 'zoho', 'external')
    journal = TodoJournal()
    assert journal.reserve('calendar-task:project:one', 'baseline')
    assert existing.get('calendar-task:project:one', 'zoho')['externalId'] == 'external'


def test_Calendar結果不明を永続化して次回の開始を拒否(local):
    journal = TodoJournal()
    key = 'calendar-task:project:one'
    assert journal.calendar_reserve(key, 'request-one')
    assert TodoJournal().calendar_get(key)['state'] == 'reserved'
    assert not TodoJournal().calendar_reserve(key, 'request-two')
    journal.calendar_finish(key, event_id='event')
    assert journal.calendar_get(key)['state'] == 'created'
    assert journal.calendar_reserve(key, 'request-two')
    journal.calendar_finish(key, skipped='未接続なので未書込')
    assert journal.calendar_get(key)['state'] == 'blocked'
    assert journal.calendar_reserve(key, 'request-three')


def test_台帳確定失敗の照合用IDは再保留でも消さない(local):
    journal = TodoJournal()
    key = 'calendar-task:project:one'
    journal.reserve(key, 'baseline')
    journal.hold(key, '作成ID確定失敗', page_id='known-page')
    journal.hold(key, '結果不明のため再実行を保留')
    review = PostgresCreationJournal().get(key, 'notion_todo_review')
    assert review['externalId'] == 'known-page'
    assert journal.get(key)['state'] == 'reserved'


def test_同入力の再開位置を永続化し入力変更なら先頭へ戻す(local):
    key = 'calendar-task-plan:project'
    TodoJournal().batch_advance(key, 'plan-one', 2)
    assert TodoJournal().batch_position(key, 'plan-one') == 2
    assert TodoJournal().batch_position(key, 'plan-two') == 0
    TodoJournal().batch_advance(key, 'plan-one', 0)
    assert TodoJournal().batch_position(key, 'plan-one') == 0


def test_原本時刻は再開位置更新でも巻戻らず古いスナップショットを拒否する(local):
    key = 'calendar-task-plan:project'
    journal = TodoJournal()
    assert journal.accept_source(key, '2026-11-25T02:00:00+00:00', 'source-new') == 'accepted'
    journal.batch_advance(key, 'plan-new', 2)
    restarted = TodoJournal()
    assert restarted.accept_source(key, '2026-11-25T01:00:00+00:00', 'source-old') == 'stale'
    assert restarted.accept_source(key, '2026-11-25T02:00:00+00:00', 'source-new') == 'accepted'
    assert restarted.batch_position(key, 'plan-new') == 2
    assert restarted.accept_source(key, '2026-11-25T02:00:00+00:00', 'different') == 'conflict'
    assert restarted.accept_source(key, '2026-11-25T03:00:00+00:00', 'source-next') == 'accepted'
    assert restarted.batch_position(key, 'plan-new') == 0
    with connect() as conn:
        saved = conn.execute('''SELECT reason::jsonb->>'accepted_source_at' AS stamp
                               FROM "HubCreationAttempt" WHERE "sourceKey"=%s AND target='calendar_task_batch' ''',
                             (key,)).fetchone()
        assert saved['stamp'] == '2026-11-25T03:00:00+00:00'
