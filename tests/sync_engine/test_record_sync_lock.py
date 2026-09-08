"""同じレコードの競合、途中失敗後の再送、古い読み取りを検証する。"""
from dataclasses import replace
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from src.db_schema.base import Tool
from src.sync_engine.dispatcher import Dispatcher
from src.sync_engine.record_sync_lock import (
    RecordSyncBusy, RecordSyncConfigurationError, RecordSyncGuard,
    acquire_record_sync_lock, lock_key,
)
from src.sync_engine.spreadsheet_row_lock import lock_key as row_lock_key
from src.sync_engine.sync_event import SyncEvent
from tests.sync_engine.test_dispatcher import NOW, _all_targets, store, mapping


def event(at=NOW):
    return SyncEvent(source_tool=Tool.NOTION, db_key="client_master", external_id="CLI-001",
                     occurred_at=at, properties={"取引先名": "変更後"})


def test_並行する同じレコードは書かず再送できる(store, mapping):
    targets = _all_targets()
    dispatcher = Dispatcher(store, targets)
    entered, release = Event(), Event()
    original = targets[Tool.KINTONE].get_record

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    targets[Tool.KINTONE].get_record = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(dispatcher.dispatch, event())
        assert entered.wait(3)
        try:
            with pytest.raises(RecordSyncBusy):
                dispatcher.dispatch(event(NOW + timedelta(seconds=1)))
            assert not targets[Tool.KINTONE].upsert_calls
        finally:
            release.set()
        assert not running.result().skipped
    assert not dispatcher.dispatch(event(NOW + timedelta(seconds=1))).skipped


def test_保存後の古いストア読み取りでも巻き戻さない(store, mapping, monkeypatch):
    dispatcher = Dispatcher(store, _all_targets())
    assert not dispatcher.dispatch(event()).skipped
    monkeypatch.setattr(store, 'get', lambda key: mapping)
    assert dispatcher.dispatch(event(NOW - timedelta(seconds=1))).reason == 'stale_event'
    assert dispatcher.dispatch(event()).reason == 'stale_event'


def test_完了印の保存失敗でも古い更新を拒否し同時刻再送を許す(store, mapping, monkeypatch):
    targets = _all_targets()
    dispatcher = Dispatcher(store, targets)
    original = RecordSyncGuard.advance

    def fail(*args):
        raise RuntimeError('完了印保存の失敗')

    monkeypatch.setattr(RecordSyncGuard, 'advance', fail)
    with pytest.raises(RuntimeError):
        dispatcher.dispatch(event())
    assert targets[Tool.KINTONE].upsert_calls
    assert dispatcher.dispatch(event(NOW - timedelta(seconds=1))).reason == 'stale_event'
    monkeypatch.setattr(RecordSyncGuard, 'advance', original)
    assert not dispatcher.dispatch(event()).skipped


def test_受理保存が失敗したら外部書込ゼロ(store, mapping, monkeypatch):
    targets = _all_targets()
    def fail(*args):
        raise RuntimeError('受理印保存の失敗')
    monkeypatch.setattr(RecordSyncGuard, 'accept', fail)
    with pytest.raises(RuntimeError):
        Dispatcher(store, targets).dispatch(event())
    assert all(not target.upsert_calls for target in targets.values())


@pytest.mark.parametrize('url', [None, 'postgresql://user@ep-example-pooler.example/db'])
def test_本番接続の未設定とpooled接続を拒否(monkeypatch, url):
    monkeypatch.delenv('DATABASE_URL_UNPOOLED', raising=False)
    if url:
        monkeypatch.setenv('DATABASE_URL_UNPOOLED', url)
    with pytest.raises(RecordSyncConfigurationError):
        with acquire_record_sync_lock(object(), 'db', 'key'):
            pytest.fail('安全でない接続で書き込めてはいけない')


def test_別レコードと行作成のロックを分離(store):
    assert lock_key('db', 'key') != row_lock_key('db', 'key')
    with acquire_record_sync_lock(store, 'db', 'key'):
        with acquire_record_sync_lock(store, 'db', 'other'):
            pass


@pytest.mark.parametrize('queue_succeeds', [True, False])
@pytest.mark.parametrize('row_creation_allowed', [True, False])
def test_新規登録直後に新しい更新が完了したら旧値の行を作らない(
    store, monkeypatch, queue_succeeds, row_creation_allowed,
):
    from tests.sync_engine.test_dispatcher import FakeSyncTarget, SpyNotifier, _kintone_client_master_record
    monkeypatch.setenv('AUTO_CREATE_NEW_RECORDS_ENABLED', 'true')
    targets = _all_targets()
    targets[Tool.KINTONE] = FakeSyncTarget(
        Tool.KINTONE, {'new-record': _kintone_client_master_record()},
    )
    notifier = SpyNotifier()
    dispatcher = Dispatcher(store, targets, slack_notifier=notifier)
    monkeypatch.setattr('src.sync_engine.dispatcher._supports_sync_key', lambda target: True)
    monkeypatch.setattr('src.sync_engine.dispatcher._row_creation_allowed', lambda *a: row_creation_allowed)
    register = dispatcher._register_new_record_mapping
    appended, queued = [], []

    def register_and_newer(mapping):
        result = register(mapping)
        with acquire_record_sync_lock(store, mapping.db_key, mapping.notion_key) as guard:
            guard.advance(NOW + timedelta(seconds=1))
        return result

    monkeypatch.setattr(dispatcher, '_register_new_record_mapping', register_and_newer)
    monkeypatch.setattr(dispatcher, '_append_spreadsheet_row_for_created_record', lambda *a: appended.append(a))
    def enqueue(**kwargs):
        queued.append(kwargs)
        return queue_succeeds

    monkeypatch.setattr('src.sync_engine.dispatcher.enqueue_row_creation', enqueue)
    result = dispatcher.dispatch(SyncEvent(
        source_tool=Tool.KINTONE, db_key='client_master', external_id='new-record',
        occurred_at=NOW, properties={},
    ))
    assert not result.skipped
    assert not appended
    assert bool(queued) == row_creation_allowed
    assert bool(notifier.new_record_issue_calls) == row_creation_allowed
    if row_creation_allowed:
        detail = notifier.new_record_issue_calls[0]['detail']
        assert ('再試行キューに積みました' if queue_succeeds else '自動では復旧しない') in detail


def test_outboxは同期中のレコードを差し戻して値を読まない(store, mapping, monkeypatch):
    from src.sync_engine import spreadsheet_outbox, spreadsheet_outbox_drain
    from types import SimpleNamespace
    released = []
    monkeypatch.setattr(spreadsheet_outbox, 'release', lambda **kw: released.append(kw))
    entry = SimpleNamespace(db_key=mapping.db_key, notion_key=mapping.notion_key)
    with acquire_record_sync_lock(store, mapping.db_key, mapping.notion_key):
        result = spreadsheet_outbox_drain._repair_one(
            entry, store=store, notion_clients={}, spreadsheet_targets={}, slack_notifier=None,
        )
    assert result == 'deferred'
    assert released == [dict(db_key=mapping.db_key, notion_key=mapping.notion_key, retry_after_minutes=1)]


def test_環境変数のpooledホストを暗黙使用しない(monkeypatch):
    monkeypatch.setenv('DATABASE_URL_UNPOOLED', 'dbname=example user=test')
    monkeypatch.setenv('PGHOST', 'ep-example-pooler.example')
    with pytest.raises(RecordSyncConfigurationError, match='ホストの明示'):
        with acquire_record_sync_lock(object(), 'db', 'key'):
            pytest.fail('暗黙ホストを使用してはいけない')


@pytest.mark.parametrize('failure', ['missing_table', 'permissions'])
def test_outbox共通設定不備ではキュー取得前に失敗する(monkeypatch, failure):
    from unittest.mock import MagicMock
    from src.sync_engine import record_sync_lock, spreadsheet_outbox_drain
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    if failure == 'missing_table':
        cur.execute.side_effect = RuntimeError('テーブル未配備')
    else:
        cur.fetchone.return_value = {'ready': False}
    monkeypatch.setattr(record_sync_lock, '_connect_direct', lambda: conn)
    monkeypatch.setattr(spreadsheet_outbox_drain, 'spreadsheet_row_creation_enabled', lambda key: True)
    claim = MagicMock()
    monkeypatch.setattr(spreadsheet_outbox_drain.spreadsheet_outbox, 'claim_due', claim)
    with pytest.raises((RecordSyncConfigurationError, RuntimeError)):
        spreadsheet_outbox_drain.drain_spreadsheet_outbox(store=object())
    claim.assert_not_called()
    conn.close.assert_called_once()


def test_事前検査は行を変更せず権限を確認する(monkeypatch):
    from unittest.mock import MagicMock
    from src.sync_engine import record_sync_lock
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = {'ready': True}
    monkeypatch.setattr(record_sync_lock, '_connect_direct', lambda: conn)
    record_sync_lock.validate_record_sync_storage(object())
    assert len(cur.execute.call_args_list) == 2
    assert all(call.args[0].startswith('SELECT ') for call in cur.execute.call_args_list)
    conn.close.assert_called_once()
