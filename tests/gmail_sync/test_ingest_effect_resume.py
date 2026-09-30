"""メール保存後のNotion失敗から再送で復帰し、通知を二重実行しない。"""
import pytest
from contextlib import contextmanager

from src.gmail_sync import gmail_client, sync


def test_保存後失敗を再送で再開する(monkeypatch):
    message = gmail_client.GmailMessage(
        id='mail-1', from_header='buyer@customer.com', to_header='rep@example.com',
        subject='相談', date_header=None, snippet='本文', internal_date_ms='1780000000000',
    )
    state = {'notionDone': False, 'incidentAttempted': False, 'engagementAttempted': False}
    saved = [False]
    def insert_once(**_values):
        if saved[0]:
            return False
        saved[0] = True
        return True
    monkeypatch.setattr(sync.db, 'insert_email_log_once', insert_once)
    monkeypatch.setattr(sync.db, 'gmail_ingest_effect_status', lambda _id: dict(state) if saved[0] else None)
    def get_log(_id):
        return {
            'contactPageId': 'contact-1', 'contactEmail': 'buyer@customer.com',
            'repEmail': 'rep@example.com', 'direction': 'inbound',
            'sentAt': sync._parse_sent_at(message), 'incidentScore': 0,
            'incidentPriority': None, 'subject': '相談', 'snippet': '本文',
        }
    monkeypatch.setattr(sync.db, 'get_email_log', get_log)
    @contextmanager
    def locked(_id):
        yield sync._parse_sent_at(message)
    monkeypatch.setattr(sync.db, 'locked_latest_email_at', locked)
    monkeypatch.setattr(sync.db, 'mark_gmail_ingest_effect', lambda _id, column: state.__setitem__(column, True))
    def claim(_id, column):
        if state[column]:
            return False
        state[column] = True
        return True
    monkeypatch.setattr(sync.db, 'claim_gmail_ingest_notification', claim)
    monkeypatch.setattr(sync, 'score_email', lambda *_: (0, None))
    delivered = []
    monkeypatch.setattr(sync, 'notify_web_engagement_tool', lambda **kw: delivered.append(kw))
    class Client:
        calls = 0
        def update_page(self, *_):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('Notionが一時停止')
    client = Client()
    kwargs = dict(internal_domains=frozenset({'example.com'}), atomic_insert=True,
                  resolve_contact=lambda _address: 'contact-1')
    with pytest.raises(RuntimeError):
        sync.record_message(message, 'rep@example.com', client, **kwargs)
    assert delivered == []
    assert sync.record_message(message, 'rep@example.com', client, **kwargs) is False
    assert state['notionDone'] and state['engagementAttempted']
    assert len(delivered) == 1
    assert sync.record_message(message, 'rep@example.com', client, **kwargs) is False
    assert len(delivered) == 1
