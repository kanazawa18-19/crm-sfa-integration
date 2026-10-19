"""引用の除去と、今回の本文を誤って削らないことを検証する。"""
import base64
import pytest
from src.email_review.body import current_body
from src.email_review.mime import plain_text
from src.email_review.domain import participants


@pytest.mark.parametrize('marker', [
    '2026年10月16日(金) 14:50 担当 <person@example.test>:',
    'On Fri, 16 Oct 2026 at 14:50 Someone wrote:',
    '担当様は書きました：',
    '-----Original Message-----',
    '---------- Forwarded message ---------',
    'From: person@example.test\r\nSent: Friday\r\nTo: me@example.test\r\nSubject: 前回',
])
def test_quote_removed_before_length_limit(marker):
    body = '今回の本文\r\n\r\n-- \r\n署名'
    text = body + '\r\n\r\n' + marker + '\r\n' + '過去' * 15000
    assert current_body(text) == body
    payload = {'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}}
    assert plain_text(payload) == body


@pytest.mark.parametrize('body', [
    '今回の本文\r\n\r\n---\r\n署名\r\n',
    'From: の項目を設定してください\n日付は2026年10月16日です。\n',
    '2026年10月16日(金) 14:50 打合せ開始\nよろしくお願いします。',
])
def test_non_quotes_preserved(body):
    assert current_body(body) == body


def test_inline_answers_preserved_and_quote_only_is_empty():
    assert current_body('> 質問\n回答\n> 別の質問\n別の回答') == '回答\n別の回答'
    assert current_body('> 過去の本文\n') == ''
    assert current_body('On Friday, A wrote:\n> 質問\n回答\n--\n署名') == '回答\n--\n署名'


@pytest.mark.parametrize('to,cc,expected', [
    ('me@cnctor.jp', '', []),
    ('sales@cnctor.jp', 'me@cnctor.jp', []),
    ('me@cnctor.jp, SALES@CNCTOR.JP', '', []),
    ('client@example.test', 'sales@cnctor.jp, me@cnctor.jp', ['client@example.test']),
    ('sales@cnctor.jp', 'client@example.test', ['client@example.test']),
    ('colleague@cnctor.jp', '', ['colleague@cnctor.jp']),
])
def test_sent_recipients_exclude_only_self_and_sales(to, cc, expected):
    direction, people = participants({'from_header': 'me@cnctor.jp', 'to_header': to, 'cc_header': cc}, 'me@cnctor.jp', {'cnctor.jp'})
    assert direction == 'outbound'
    assert [p['email'] for p in people] == expected


def test_gateway_removes_quotes_from_previously_captured_mail():
    from src.email_review.gateway import Gateway
    row = {'id': 'mail', 'direction': 'outbound', 'contact_name': '担当', 'company_id': 'company',
           'message': {'label_ids': ['SENT'], 'body_text': '今回\nOn Friday, A wrote:\n' + '過去' * 15000, 'internal_date_ms': '1792130400000'}}
    assert Gateway().action_properties(row)['履歴メモ'] == '今回'


def test_same_owner_recapture_accepts_quote_migration_but_not_different_body_or_owner():
    from src.email_review.repository import same_mail
    old = {'body_text': '今回\nOn Friday, A wrote:\n以前の本文'}
    new = {'body_text': '今回'}
    assert same_mail(old, new, allow_incomplete=True)
    assert not same_mail(old, new)
    assert not same_mail(old, {'body_text': '別の本文'}, allow_incomplete=True)
    assert not same_mail({'body_text': '> 引用だけ'}, {'body_text': '> 別の引用'}, allow_incomplete=True)
