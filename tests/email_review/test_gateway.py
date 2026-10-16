import pytest
from src.email_review.gateway import Gateway
from src.email_review.domain import Conflict, same_value


def test_cannot_reassign_other_contact_using_previous_link_proof(monkeypatch):
    gateway = Gateway()
    monkeypatch.setattr(gateway, 'live', lambda key, id: {'メールアドレス': 'b@example.test', '取引先マスター': ['old']} if key == 'contact' else {'取引先名': '会社'})
    row = {'channel': 'dashboard', 'linked_company_id': 'old', 'linked_contact_id': 'A', 'participants': [{'email': 'b@example.test'}]}
    with pytest.raises(Conflict):
        gateway.validate(row, {'company_id': 'new', 'contact_id': 'B'})


def test_notion_date_normalization_does_not_prevent_recovery():
    assert same_value('アクション日', '2026-10-16T01:00:00.000+09:00', '2026-10-15T16:00:00Z')
    assert not same_value('アクション日', '2026-10-16T01:00:00+09:00', '2026-10-16T01:00:00Z')
