"""合成メールだけで照合と重複・古い画面を検証する。"""
import base64
import pytest
from src.email_review.domain import evidence, match, decide, identity, participants, Conflict
from src.email_review.mime import plain_text


@pytest.mark.parametrize('sender,subject', [
    ('no-reply@account.example.test', 'Partner Portalへのログイン用ワンタイムパスコード'),
    ('no-reply@example.test', 'Your one-time passcode'),
    ('noreply@tiket.com', 'Kode OTP konfirmasi akun'),
    ('"tiket.com" <noreply@tiket.com>', 'Log in from a new device'),
    ('info@willer.co.jp', '認証コードを送付します'),
    ('INFO@WILLER.CO.JP', '認証コードを送付します'),
    ('info@kosokubus.com', '【高速バスドットコム】パスワード通知メール'),
    ('UPSIDER <support@up-sider.com>', 'UPSIDER |【重要】本人認証手続き：取引を承認または拒否してください'),
])
def test_authentication_notice_variants(sender, subject):
    from src.email_review.domain import authentication_notice
    assert authentication_notice({'from_header': sender, 'subject': subject})


@pytest.mark.parametrize('sender,subject', [
    ('person@example.test', '認証コードの仕様について相談'),
    ('noreply@tiket.com', 'Booking confirmation'),
    ('person@example.test', 'Kode OTP konfirmasi akun'),
    ('noreply@tiket.com', 'Re: Log in from a new device'),
    ('noreply@tiket.com, person@example.test', 'Kode OTP konfirmasi akun'),
    ('info@willer.co.jp', '認証コードを送付しますが、仕様について相談です'),
    ('info@kosokubus.com', '予約受付確認書'),
    ('support@up-sider.com', '本人認証手続きについてのご相談'),
    ('support@another.test', 'UPSIDER |【重要】本人認証手続き：取引を承認または拒否してください'),
    ('no-reply@example.test', 'お問い合わせを受け付けました'),
    ('info@willer.co.jp, person@example.test', '認証コードを送付します'),
    ('', ''),
])
def test_authentication_filter_preserves_other_mail(sender, subject):
    from src.email_review.domain import authentication_notice
    assert not authentication_notice({'from_header': sender, 'subject': subject})


def test_same_company_in_salutation_and_signature_is_two_evidence():
    sent = {'id': 's1', 'body_text': 'テスト株式会社\n山田様\nご相談です。\n---\n自社株式会社'}
    received = {'id': 'r1', 'body_text': '金沢様\nご返信ありがとうございます。\n---\nテスト株式会社\n山田\nyamada@example.test'}
    rows = evidence(sent, 'outbound', 'yamada@example.test') + evidence(received, 'inbound', 'yamada@example.test')
    result = match(rows, [{'id': 'company', 'name': 'テスト株式会社'}], [])
    assert result['state'] == 'approved'
    assert result['company_id'] == 'company'
    assert result['candidates'][0]['sources'] == ['salutation', 'signature']


def test_repeated_salutation_is_one_and_conflicting_affiliation_is_pending():
    a = {'source': 'salutation', 'value': 'テスト株式会社', 'message_id': 's1'}
    b = {**a, 'source': 'signature', 'message_id': 'r1'}
    masters = [{'id': 'a', 'name': 'テスト株式会社'}]
    assert match([a, a], masters, [])['state'] == 'pending'
    assert match([a, b], masters, ['b'])['state'] == 'pending'
    assert match([a, b], masters + [{'id': 'b', 'name': 'テスト株式会社'}], [])['state'] == 'pending'


def test_own_signature_and_forward_are_not_counterparty_evidence():
    msg = {'id': 's', 'body_text': '相手株式会社\n田中様\n本文\n---\n自社株式会社\nme@own.test\n> 別会社'}
    rows = evidence(msg, 'outbound', 'other@external.test')
    assert not any(e['value'] == '自社株式会社' for e in rows)
    received = {'id': 'r', 'body_text': '本文\nFrom: forwarded@external.test\n---\n別会社\nx@external.test'}
    assert evidence(received, 'inbound', 'x@external.test') == []


def test_multiple_participants_including_cc_are_kept():
    direction, people = participants({'from_header': 'Me <me@own.test>', 'to_header': 'A <a@external.test>', 'cc_header': 'B <b@external.test>, colleague@own.test'}, 'me@own.test', {'own.test'})
    assert direction == 'outbound'
    assert [p['email'] for p in people] == ['a@external.test', 'b@external.test', 'colleague@own.test']


def test_revision_and_processing_protect_old_slack_buttons():
    row = {'id': 'a', 'direction': 'outbound', 'revision': 2, 'state': 'pending'}
    args = dict(action='confirm', selection={'company_id': 'c', 'contact_id': 'p'}, actor='u', channel='slack')
    with pytest.raises(Conflict):
        decide(row, revision=1, **args)
    with pytest.raises(Conflict):
        decide({**row, 'state': 'processing'}, revision=2, **args)
    new = decide(row, revision=2, **args)
    assert new['revision'] == 3 and new['state'] == 'approved'


def test_identity_across_mailboxes_and_fallback():
    msg = {'id': 'a', 'message_id_header': '<same@example.test>', 'from_header': 'sender@example.test', 'date_header': '2026-10-16'}
    assert identity(msg, 'one@own.test') == identity({**msg, 'id': 'b'}, 'two@own.test')
    assert identity({'id': 'a'}, 'one@own.test') != identity({'id': 'a'}, 'two@own.test')


def test_mime_ignores_attachment_html_and_large_body():
    def part(text, **extra):
        return {'mimeType': 'text/plain', 'body': {'data': base64.urlsafe_b64encode(text.encode()).decode()}, **extra}
    assert plain_text({'mimeType': 'multipart/mixed', 'parts': [part('本文'), part('添付', filename='a.txt'), part('HTML', mimeType='text/html')]}) == '本文'
    assert plain_text(part('x' * 20001)) == ''
    assert plain_text(part('添付', mimeType='message/rfc822')) == ''


def test_same_rfc_mail_ignores_from_display_name_and_date_format():
    first = {'id': 'a', 'message_id_header': '<same@example.test>', 'from_header': '名前 <sender@example.test>', 'date_header': 'Fri, 16 Oct 2026 10:00:00 +0900'}
    second = {**first, 'id': 'b', 'from_header': 'sender@example.test', 'date_header': '16 Oct 2026 01:00:00 GMT'}
    assert identity(first, 'one@example.test') == identity(second, 'two@example.test')


def test_japanese_charset_is_decoded():
    text = '試験株式会社\n担当様'
    payload = {'mimeType': 'text/plain', 'headers': [{'name': 'Content-Type', 'value': 'text/plain; charset=ISO-2022-JP'}], 'body': {'data': base64.urlsafe_b64encode(text.encode('iso-2022-jp')).decode()}}
    assert plain_text(payload) == text


@pytest.mark.parametrize('action', ['confirm', 'resume'])
@pytest.mark.parametrize('direction', ['inbound', None])
def test_received_mail_cannot_be_confirmed_or_resumed_from_either_ui(action, direction):
    row = {'id': 'r', 'revision': 1, 'state': 'pending', 'direction': direction}
    with pytest.raises(Conflict, match='送信メールだけ'):
        decide(row, revision=1, action=action, selection={'company_id': 'c', 'contact_id': 'p'}, actor='u', channel='slack')
