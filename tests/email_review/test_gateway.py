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


def test_history_memo_preserves_full_body_and_notion_chunks_without_summary():
    from src.sync_engine.clients.notion_client import _rich_text_content
    body = '会社名\r\n本文と署名、引用もそのまま。\n' * 500
    row = {'id': 'mail-id', 'message': {'label_ids': ['SENT'], 'body_text': body, 'snippet': '短い抜粋', 'subject': '件名', 'internal_date_ms': '1792130400000'},
           'direction': 'outbound', 'contact_name': '相手', 'company_id': 'company'}
    properties = Gateway().action_properties(row)
    assert properties['履歴メモ'] == body.replace('\r\n', '\n')
    chunks = _rich_text_content(properties['履歴メモ'])
    assert len(chunks) > 1
    assert all(len(chunk['text']['content']) <= 2000 for chunk in chunks)
    assert ''.join(chunk['text']['content'] for chunk in chunks) == body.replace('\r\n', '\n')


@pytest.mark.parametrize('body', [None, '', '  \n'])
def test_missing_full_body_never_falls_back_to_snippet(body):
    with pytest.raises(Conflict, match='全文'):
        Gateway().action_properties({'direction': 'outbound', 'message': {'label_ids': ['SENT'], 'body_text': body, 'snippet': '抜粋だけ'}})


def test_partial_multipart_body_is_not_recorded_as_full_text():
    import base64
    from src.email_review.mime import plain_text
    def part(text):
        return {'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}}
    payload = {'mimeType': 'multipart/mixed', 'parts': [part('先頭だけ'), part('長' * 40001)]}
    assert plain_text(payload) == ''
    payload['parts'] = [part('先頭だけ'), {**part('本文'), 'headers': [{'name': 'Content-Type', 'value': 'text/plain; charset=unknown'}]}]
    assert plain_text(payload) == ''
    payload['parts'] = [part('本文')] * 31
    assert plain_text(payload) == ''


def test_received_mail_cannot_produce_action_properties():
    with pytest.raises(Conflict, match='送信メールだけ'):
        Gateway().action_properties({'direction': 'inbound', 'message': {'label_ids': ['SENT'], 'body_text': '受信本文'}})
