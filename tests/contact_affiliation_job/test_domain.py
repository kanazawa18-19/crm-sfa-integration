"""2原本・矛盾・期限・人物同一性を検証する。"""
from datetime import datetime, timezone
import pytest
from src.contact_affiliation_job.domain import decide
from src.contact_affiliation_job.evidence import mail_evidence, company_lines, zoho_evidence

NOW = datetime(2026, 10, 19, tzinfo=timezone.utc)
CONTACT = {'メールアドレス': 'person@example.invalid'}
MASTERS = [{'id': 'company', 'name': '試験株式会社'}]


def proof(origin, **changes):
    return dict({'email': 'person@example.invalid', 'origin': origin, 'source_id': origin + ':1',
                 'observed_at': '2026-10-18T00:00:00Z', 'company_name': '試験株式会社', 'explicit': True}, **changes)


def test_independent_two_sources():
    assert decide(CONTACT, [proof('a'), proof('b')], MASTERS, NOW)['state'] == 'ready'


@pytest.mark.parametrize('rows', [[], [proof('a')], [proof('a'), proof('a', source_id='a:2')],
    [proof('a'), proof('b', email='other@example.invalid')], [proof('a'), proof('b', explicit=False)],
    [proof('a'), proof('b', observed_at='2020-01-01T00:00:00Z')],
    [proof('a'), proof('b', observed_at='2027-01-01T00:00:00Z')],
    [proof('a'), proof('b'), proof('c', company_name='別の株式会社')]])
def test_unsafe_evidence_held(rows):
    assert decide(CONTACT, rows, MASTERS, NOW)['state'] == 'held'


@pytest.mark.parametrize('contact', [{**CONTACT, '取引先マスター': ['existing']}, {**CONTACT, '人事異動フラグ': True}, {}])
def test_existing_affiliation_or_transfer_not_written(contact):
    assert decide(contact, [proof('a'), proof('b')], MASTERS, NOW)['state'] == 'held'


def test_duplicate_company_names_held():
    assert decide(CONTACT, [proof('a'), proof('b')], MASTERS + [{'id': 'other', 'name': '試験株式会社'}], NOW)['state'] == 'held'


def mail(direction='inbound', **changes):
    message = dict(id='mail1', body_text='本文\n---\n試験株式会社\n試験 太郎\nperson@example.invalid',
        from_header='person@example.invalid', to_header='sales@example.invalid', label_ids=['INBOX'],
        internal_date_ms=1792281600000)
    message.update(changes)
    return {'id': 'review1', 'direction': direction, 'message': message}


def test_inbound_requires_own_signature_email():
    good = mail_evidence([mail()], CONTACT['メールアドレス'], ['試験 太郎'])
    assert len(company_lines(good, MASTERS)) == 1
    assert not mail_evidence([mail(body_text='本文\n---\n試験株式会社\n別人\nother@example.invalid')], CONTACT['メールアドレス'])
    assert not mail_evidence([mail(from_header='other@example.invalid')], CONTACT['メールアドレス'])


def test_outbound_requires_sent_and_one_recipient():
    valid = mail('outbound', from_header='sales@example.invalid', to_header=CONTACT['メールアドレス'],
                 body_text='試験株式会社\n試験様\nこんにちは', label_ids=['SENT'])
    assert len(company_lines(mail_evidence([valid], CONTACT['メールアドレス']), MASTERS)) == 1
    for change in [{'label_ids': ['DRAFT']}, {'label_ids': []}, {'to_header': 'a@example.invalid,b@example.invalid'},
                   {'cc_header': 'other@example.invalid'}]:
        changed = {**valid, 'message': {**valid['message'], **change}}
        assert not mail_evidence([changed], CONTACT['メールアドレス'])


def test_unknown_explicit_company_is_conflict_not_dropped():
    rows = [proof('a'), proof('b'), proof('c', company_name='別の株式会社')]
    assert len(company_lines(rows, MASTERS)) == 3


def test_zoho_eight_and_lookup_are_one_origin():
    rows = zoho_evidence({'id': '1', 'Email': CONTACT['メールアドレス'], 'field21': '試験株式会社',
        'field25': {'id': 'lookup', 'name': '試験株式会社'}, 'Modified_Time': '2026-10-18T00:00:00Z'}, CONTACT['メールアドレス'], 'company')
    assert len(rows) == 2
    assert decide(CONTACT, rows, MASTERS, NOW)['state'] == 'held'
    assert decide(CONTACT, rows + [proof('mail')], MASTERS, NOW)['state'] == 'held'
    assert decide(CONTACT, rows + [proof('mail'), proof('form')], MASTERS, NOW)['state'] == 'ready'


@pytest.mark.parametrize('body', [
    '今回ご紹介する会社はこちらです。\n試験株式会社\n所属はしていませんが、私から担当者へ連絡します。\n返信先: person@example.invalid',
    '---\n試験株式会社\n試験 太郎\notherperson@example.invalid',
    '---\n試験株式会社\n試験 太郎\nperson@example.invalid.other',
    '---\n試験株式会社\n別人 太郎\nperson@example.invalid',
    '---\n試験株式会社\n試験 太郎\n所属していません\nperson@example.invalid',
    '---\nご紹介先です\n試験株式会社\n試験 太郎\nperson@example.invalid',
])
def test_intro_or_other_person_never_becomes_signature(body):
    assert mail_evidence([mail(body_text=body)], CONTACT['メールアドレス'], ['試験 太郎']) == []


@pytest.mark.parametrize('resolved', [None, 'different-company'])
def test_lookup_id_must_resolve_to_same_candidate(resolved):
    rows = zoho_evidence({'id': 'contact', 'Email': CONTACT['メールアドレス'],
        'field25': {'id': 'foreign-id', 'name': '試験株式会社'},
        'Modified_Time': '2026-10-18T00:00:00Z'}, CONTACT['メールアドレス'], resolved)
    assert rows[0]['lookup_id'] == 'foreign-id'
    assert decide(CONTACT, rows + [proof('mail')], MASTERS, NOW)['state'] == 'held'


@pytest.mark.parametrize('stamp', [None, 123, {}, 'bad'])
def test_missing_or_malformed_source_date_does_not_crash(stamp):
    assert decide(CONTACT, [proof('a'), proof('b', observed_at=stamp)], MASTERS, NOW)['state'] == 'held'


def test_zoho_unrelated_update_never_refreshes_affiliation_vote():
    rows = zoho_evidence({'id': '1', 'Email': CONTACT['メールアドレス'], 'field21': '試験株式会社',
        'Modified_Time': NOW.isoformat()}, CONTACT['メールアドレス'])
    assert rows[0]['check_only'] is True
    assert decide(CONTACT, rows + [proof('mail')], MASTERS, NOW)['state'] == 'held'
    rows[0]['company_name'] = '別の株式会社'
    rows[0]['observed_at'] = None
    assert decide(CONTACT, rows + [proof('mail'), proof('form')], MASTERS, NOW)['state'] == 'held'
