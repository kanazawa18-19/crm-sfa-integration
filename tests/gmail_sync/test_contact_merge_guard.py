"""通常メール保存と連絡先統合が共通の鍵・旧ID解決を使うことを確認する。"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from src.gmail_sync import sync
from src.gmail_sync.gmail_client import GmailMessage
from src.record_merge import aliases
from src.sync_engine import record_sync_lock


def message():
    return GmailMessage(label_ids=['SENT'], id='message', from_header='customer@example.test', to_header='rep@internal.test',
                        subject='合成', snippet='本文', date_header=None, internal_date_ms='1780000000000')


@pytest.fixture
def effects(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, 'score_email', lambda *_: (0, None))
    monkeypatch.setattr(sync.db, 'insert_email_log', lambda **kw: calls.append(('insert', kw['contact_page_id'])))
    monkeypatch.setattr(sync, 'notify_web_engagement_tool', lambda **_: None)
    @contextmanager
    def latest(page_id):
        yield sync._parse_sent_at(message())
    monkeypatch.setattr(sync.db, 'locked_latest_email_at', latest)
    client = SimpleNamespace(update_page=lambda page, _: calls.append(('update', page)))
    return calls, client


def test_旧IDから正本へ誘導し保存と最終日時まで鍵を保持(monkeypatch, effects):
    calls, client = effects
    active = set()
    @contextmanager
    def locks(_store, keys):
        active.update(keys)
        try:
            yield
        finally:
            active.clear()
    monkeypatch.setattr(record_sync_lock, 'acquire_record_sync_locks', locks)
    monkeypatch.setattr(aliases, 'resolve_alias', lambda _db, _tool, identifier: 'target' if identifier == 'source' else None)
    monkeypatch.setattr(aliases, 'require_record_available', lambda _db, identifier: None)
    def insert(**kw):
        assert active == {('contact', 'source'), ('contact', 'target')}
        calls.append(('insert', kw['contact_page_id']))
    def update(page, _values):
        assert ('contact', page) in active
        calls.append(('update', page))
    monkeypatch.setattr(sync.db, 'insert_email_log', insert)
    client.update_page = update
    assert sync.record_message(message(), 'rep@internal.test', client, internal_domains=frozenset({'internal.test'}),
                               resolve_contact=lambda _: 'source', capture_review=False)
    assert calls == [('insert', 'target'), ('update', 'target')]
    assert active == set()


def test_統合保留中は保存も最終日時更新もしない(monkeypatch, effects):
    calls, client = effects
    from contextlib import nullcontext
    monkeypatch.setattr(record_sync_lock, 'acquire_record_sync_locks', lambda *_: nullcontext())
    monkeypatch.setattr(aliases, 'resolve_alias', lambda *_: None)
    def busy(*_):
        raise record_sync_lock.RecordSyncBusy('統合中')
    monkeypatch.setattr(aliases, 'require_record_available', busy)
    with pytest.raises(record_sync_lock.RecordSyncBusy):
        sync.record_message(message(), 'rep@internal.test', client, internal_domains=frozenset({'internal.test'}),
                            resolve_contact=lambda _: 'source', capture_review=False, defer_busy=False)
    assert calls == []


def test_鍵の取得前に統合先が変わったら再試行する(monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(record_sync_lock, 'acquire_record_sync_locks', lambda *_: nullcontext())
    answers = iter([None, 'target'])
    monkeypatch.setattr(aliases, 'resolve_alias', lambda *_: next(answers))
    with pytest.raises(record_sync_lock.RecordSyncBusy):
        with sync.contact_record_guard('source'):
            pytest.fail('古い鍵では保存しない')


def test_実DBの統合鍵と通常メール保存は競合する(history, monkeypatch):
    import os
    monkeypatch.setenv('DATABASE_URL_UNPOOLED', os.environ['DATABASE_URL'])
    with record_sync_lock.acquire_record_sync_locks(None, {('contact', 'source')}):
        with pytest.raises(record_sync_lock.RecordSyncBusy):
            with sync.contact_record_guard('source'):
                pytest.fail('統合が鍵を保持中に入れない')


# ローカルDBの隔離スキーマだけを利用する。
from tests.record_merge.test_email_history import history
from tests.record_merge.test_postgres import local
