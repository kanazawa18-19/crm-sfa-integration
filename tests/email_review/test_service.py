"""外部書込みは代替し、応答紛失時の二重作成防止と同一記録の再配送を検証。"""
from copy import deepcopy
import pytest
from src.email_review.service import Service
from src.email_review.domain import Conflict


class Repo:
    def checkpoint(self, row, **changes):
        self.row = {**row, **changes, 'revision': row['revision'] + 1}
        return deepcopy(self.row)
    finish = checkpoint


class Gateway:
    def __init__(self):
        self.created = self.updated = 0
        self.fail_create = self.fail_delivery = False
    def validate(self, row, selection):
        return {**selection, 'company_name': '会社', 'contact_name': '相手', 'target_name': '施設', 'project_name': None}
    def actor(self, owner):
        return 'notion-user'
    def link_contact(self, row):
        return {'取引先マスター': ['company']}, True
    def action_properties(self, row):
        return {'title': 'メール共通ID'}
    def create_action(self, properties):
        self.created += 1
        if self.fail_create:
            raise TimeoutError('外部応答不明')
        return 'action-id'
    def update_action(self, row, properties):
        self.updated += 1
    def dispatch(self, *args):
        if self.fail_delivery:
            raise TimeoutError()
        return 'delivered'
    def find_action(self, row):
        return 'action-id'


def test_create_timeout_reconcile_then_updates_existing_action_only():
    repo, gateway = Repo(), Gateway()
    service = Service(repo, gateway)
    gateway.fail_create = True
    result = service.process({'id': 'r', 'direction': 'outbound', 'revision': 0, 'owner': 'me@example.test', 'contact_id': 'c', 'company_id': 'company'})
    assert result['state'] == 'unknown'
    assert gateway.created == 1
    gateway.fail_create = False
    recovered = service.reconcile(result)
    assert recovered['action_id'] == 'action-id'
    result = service.process(recovered)
    assert result['delivery'] == 'delivered'
    assert gateway.created == 1 and gateway.updated == 1


def test_delivery_failure_preserves_action_and_retry_does_not_create():
    repo, gateway = Repo(), Gateway()
    service = Service(repo, gateway)
    gateway.fail_delivery = True
    result = service.process({'id': 'r', 'direction': 'outbound', 'revision': 0, 'owner': 'me@example.test', 'contact_id': 'c', 'company_id': 'company'})
    assert result['state'] == 'done' and result['delivery'] == 'partial'
    assert result['action_id'] == 'action-id'
    gateway.fail_delivery = False
    assert service.process(result)['delivery'] == 'delivered'
    assert gateway.created == 1


def test_conflicting_affiliation_stops_before_any_write():
    repo, gateway = Repo(), Gateway()
    gateway.validate = lambda *args: (_ for _ in ()).throw(Conflict('所属が変化'))
    result = Service(repo, gateway).process({'id': 'r', 'direction': 'outbound', 'revision': 0})
    assert result['state'] == 'pending'
    assert gateway.created == gateway.updated == 0


def test_additional_internal_domain_skips_company_notice_before_storage(monkeypatch):
    from unittest.mock import Mock
    from src.email_review.service import capture, internal_domains
    from src.gmail_sync.gmail_client import GmailMessage
    monkeypatch.setenv("EMAIL_REVIEW_ENABLED", "true")
    monkeypatch.setenv("EMAIL_REVIEW_START_AT", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("INTERNAL_EMAIL_DOMAINS", "example.test,existing.test")
    monkeypatch.setenv("EMAIL_REVIEW_INTERNAL_DOMAINS", " Company.test ")
    repo = Mock()
    message = GmailMessage(id="notice", from_header="system@company.test", to_header="me@example.test",
                           internal_date_ms="1792130400000", body_text="認証用本文", subject="認証", date_header=None, snippet=None)
    capture(message, "me@example.test", repo=repo)
    repo.capture.assert_not_called()
    assert internal_domains() == {"example.test", "existing.test", "company.test"}


@pytest.mark.parametrize('sender,subject', [
    ('no-reply@outside.test', '2段階認証コードのご連絡'),
    ('noreply@tiket.com', 'Kode OTP konfirmasi akun'),
    ('noreply@tiket.com', 'Log in from a new device'),
    ('no-reply@outside.test', 'Partner Portalへのログイン用ワンタイムパスコード'),
    ('info@willer.co.jp', '認証コードを送付します'),
    ('info@kosokubus.com', '【高速バスドットコム】パスワード通知メール'),
    ('support@up-sider.com', 'UPSIDER |【重要】本人認証手続き：取引を承認または拒否してください'),
])
def test_authentication_notice_is_not_stored_but_human_discussion_is(monkeypatch, sender, subject):
    from unittest.mock import Mock
    from dataclasses import replace
    from src.email_review.service import capture
    from src.gmail_sync.gmail_client import GmailMessage
    monkeypatch.setenv("EMAIL_REVIEW_ENABLED", "true")
    monkeypatch.setenv("EMAIL_REVIEW_START_AT", "2026-01-01T00:00:00+00:00")
    monkeypatch.setenv("INTERNAL_EMAIL_DOMAINS", "example.test")
    repo = Mock()
    message = GmailMessage(id="notice", from_header=sender, to_header="me@example.test",
                           internal_date_ms="1792130400000", body_text="認証用本文", subject=subject, date_header=None, snippet=None)
    capture(message, "me@example.test", repo=repo)
    repo.capture.assert_not_called()
    capture(replace(message, from_header="person@outside.test", subject="認証コードの仕様について相談"), "me@example.test", repo=repo)
    repo.capture.assert_called_once()
    repo.reset_mock()
    capture(replace(message, subject="お問い合わせの受付"), "me@example.test", repo=repo)
    repo.capture.assert_called_once()


@pytest.mark.parametrize('direction', ['inbound', None])
@pytest.mark.parametrize('existing,state', [({}, 'excluded'), ({'attempted_properties': {'title': '予約'}}, 'unknown'), ({'action_id': 'existing'}, 'held')])
def test_non_outbound_queue_never_writes_externally(direction, existing, state):
    from unittest.mock import Mock
    gateway = Mock()
    row = {'id': 'r', 'revision': 0, 'direction': direction, **existing}
    result = Service(Repo(), gateway).process(row)
    assert result['state'] == state
    assert not gateway.mock_calls
    for key, value in existing.items():
        assert result[key] == value


def test_inbound_capture_is_evidence_only_and_outbound_remains_action_candidate(monkeypatch):
    from unittest.mock import Mock
    from dataclasses import replace
    from src.email_review.service import capture
    from src.gmail_sync.gmail_client import GmailMessage
    monkeypatch.setenv('EMAIL_REVIEW_ENABLED', 'true')
    monkeypatch.setenv('EMAIL_REVIEW_START_AT', '2026-01-01T00:00:00+00:00')
    monkeypatch.setenv('INTERNAL_EMAIL_DOMAINS', 'example.test')
    repo = Mock()
    message = GmailMessage(id='received', from_header='person@outside.test', to_header='me@example.test',
                           internal_date_ms='1792130400000', body_text='会社名と署名', subject='相談', date_header=None, snippet=None)
    capture(message, 'me@example.test', repo=repo)
    received = repo.capture.call_args.args[0]
    assert received['state'] == 'excluded'
    assert received['message']['body_text'] == '会社名と署名'
    capture(replace(message, id='sent', from_header='me@example.test', to_header='person@outside.test'), 'me@example.test', repo=repo)
    assert repo.capture.call_args.args[0]['state'] == 'new'


def test_signature_does_not_fill_missing_affiliation_or_register_received_mail():
    from unittest.mock import Mock
    company, person = 'company', 'person'
    sent = {'id': 's', 'revision': 0, 'direction': 'outbound', 'canonical_key': 's',
            'participants': [{'email': 'contact@outside.test'}],
            'message': {'id': 's', 'body_text': '試験株式会社\n担当様\n本文'}}
    received = {**sent, 'id': 'r', 'direction': 'inbound', 'state': 'excluded',
                'message': {'id': 'r', 'body_text': '返信本文\n---\n試験株式会社\ncontact@outside.test'}}
    repo, gateway = Repo(), Mock()
    repo.thread = lambda row: [sent, received]
    repo.masters = lambda values: [{'id': company, 'name': '試験株式会社'}]
    gateway.contacts.return_value = [{'id': person, 'email': 'contact@outside.test', 'affiliations': []}]
    gateway.validate.return_value = {'company_id': company, 'contact_id': person}
    service = Service(repo, gateway)
    assert service.evaluate(sent)['state'] == 'pending'
    gateway.validate.assert_not_called()
    gateway.reset_mock()
    assert service.evaluate(received)['state'] == 'excluded'
    assert not gateway.mock_calls


@pytest.mark.parametrize('missing,duplicate,wrong_sender', [(False, False, False), (True, False, False), (False, True, False), (False, False, True)])
def test_single_to_email_with_cc_ignores_signature_sender(missing, duplicate, wrong_sender):
    from unittest.mock import Mock
    primary = {'email': 'a@example.test', 'name': '田中 太郎'}
    cc = {'email': 'b@example.test', 'name': '佐藤花子'}
    sent = {'id': 's', 'revision': 0, 'direction': 'outbound', 'canonical_key': 's',
            'participants': [primary, cc],
            'message': {'id': 's', 'to_header': '田中 太郎 <a@example.test>', 'body_text': '試験株式会社\n田中様\n本文'}}
    sender = cc if wrong_sender else primary
    received = {**sent, 'id': 'r', 'direction': 'inbound', 'participants': [sender],
                'message': {'id': 'r', 'body_text': '返信\n---\n試験株式会社\n' + sender['email']}}
    repo, gateway = Repo(), Mock()
    repo.thread = lambda row: [sent, received]
    repo.masters = lambda values: [{'id': 'company', 'name': '試験株式会社'}]
    contact = {'id': 'a', 'email': primary['email'], 'name': primary['name'], 'affiliations': ['company']}
    gateway.contacts.return_value = [{'id': 'b', 'email': cc['email'], 'name': cc['name'], 'affiliations': ['other']}]
    if not missing:
        gateway.contacts.return_value.append(contact)
    if duplicate:
        gateway.contacts.return_value.append({**contact, 'id': 'duplicate'})
    gateway.validate.side_effect = lambda row, selection: {**selection, 'company_name': '会社'}
    result = Service(repo, gateway).evaluate(sent)
    assert result['participants'] == [primary, cc]
    assert len(result['contacts']) == len(gateway.contacts.return_value)
    if missing or duplicate:
        assert result['state'] == 'pending'
        gateway.validate.assert_not_called()
    else:
        assert result['state'] == 'approved' and result['contact_id'] == 'a'
