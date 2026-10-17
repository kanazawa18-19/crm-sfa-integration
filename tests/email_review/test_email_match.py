"""メール優先照合と、確認後に連絡先が変わった場合の作成防止。"""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from src.email_review.domain import Conflict, match_existing_contact
from src.email_review.gateway import Gateway
from src.email_review.service import Service


def sample():
    row = {'id': 'mail', 'revision': 0, 'direction': 'outbound', 'canonical_key': 'mail',
           'participants': [{'email': 'person@example.test'}, {'email': 'cc@example.test'}],
           'message': {'to_header': '表示名なし <person@example.test>', 'body_text': '本文だけ'}}
    contacts = [{'id': 'person', 'email': 'PERSON@example.test', 'name': '同姓', 'affiliations': ['company']},
                {'id': 'cc', 'email': 'cc@example.test', 'name': '同姓', 'affiliations': ['other']}]
    return row, contacts


def test_exact_email_uses_existing_person_and_affiliation_without_name_or_signature():
    row, contacts = sample()
    result = match_existing_contact(row['message'], row['participants'], contacts)
    assert result['state'] == 'approved'
    assert result['contact_id'] == 'person'
    assert result['company_id'] == result['target_id'] == 'company'
    assert result['project_id'] is None


@pytest.mark.parametrize('change', ['duplicate', 'missing', 'empty_company', 'multiple_companies', 'multiple_to', 'only_cc'])
def test_ambiguous_or_missing_identity_is_not_registered(change):
    row, contacts = sample()
    if change == 'duplicate':
        contacts.append({**contacts[0], 'id': 'duplicate'})
    elif change == 'missing':
        contacts = contacts[1:]
    elif change == 'empty_company':
        contacts[0]['affiliations'] = []
    elif change == 'multiple_companies':
        contacts[0]['affiliations'] = ['company', 'other']
    elif change == 'multiple_to':
        row['message']['to_header'] = 'person@example.test, cc@example.test'
    else:
        row['message']['to_header'] = ''
    assert match_existing_contact(row['message'], row['participants'], contacts)['state'] == 'pending'


def test_evaluation_does_not_read_signatures_and_clears_old_target_and_project():
    row, contacts = sample()
    row.update(target_id='old-target', project_id='old-project')
    repo, gateway = Mock(), Mock()
    repo.finish.side_effect = lambda row, **changes: {**row, **changes}
    gateway.contacts.return_value = contacts
    gateway.validate.side_effect = lambda row, selection: {**selection, 'company_name': '既存会社'}
    result = Service(repo, gateway).evaluate(row)
    assert result['state'] == 'approved' and result['target_id'] == 'company'
    assert result['project_id'] is None
    assert result['match_basis'] == 'contact_email' and result['channel'] == 'automatic'
    repo.thread.assert_not_called()
    repo.masters.assert_not_called()
    row.pop('canonical_key')
    gateway.validate.reset_mock()
    assert Service(repo, gateway).evaluate(row)['state'] == 'pending'
    gateway.validate.assert_not_called()


@pytest.mark.parametrize('change', ['none', 'duplicate', 'email', 'company', 'empty_company', 'target', 'project'])
def test_creation_rechecks_live_email_and_affiliation(monkeypatch, change):
    row, contacts = sample()
    selection = {k: v for k, v in match_existing_contact(row['message'], row['participants'], contacts).items()
                 if k in ('contact_id', 'company_id', 'target_id', 'project_id')}
    row['participants'][0]['email'] = ' PERSON@example.test '
    row.update(channel='automatic', match_basis='contact_email')
    original = deepcopy(contacts)
    if change == 'duplicate': contacts.append({**contacts[0], 'id': 'duplicate'})
    if change == 'email': contacts[0]['email'] = 'changed@example.test'
    if change == 'company': contacts[0]['affiliations'] = ['other']
    if change == 'empty_company': contacts[0]['affiliations'] = []
    if change == 'target': selection['target_id'] = 'other'
    if change == 'project': selection['project_id'] = 'project'
    gateway = Gateway()
    monkeypatch.setattr(gateway, 'contacts', lambda addresses: contacts)
    monkeypatch.setattr(gateway, 'live', lambda key, pid: {'メールアドレス': 'person@example.test',
        '取引先マスター': original[0]['affiliations'], '名前': '相手'} if key == 'contact' else {'取引先名': '名称変更後の既存会社'})
    if change == 'none':
        assert gateway.validate(row, selection)['company_name'] == '名称変更後の既存会社'
    else:
        with pytest.raises(Conflict): gateway.validate(row, selection)


def test_mail_payload_matches_notion_saved_precision_without_changing_source():
    from datetime import datetime
    row, _ = sample()
    row.update(contact_name='相手', company_id='company')
    row['message'].update(body_text='本文\r\n署名\r\n', internal_date_ms=str(int(datetime.fromisoformat('2026-10-17T10:15:49+09:00').timestamp()*1000)))
    before = deepcopy(row)
    props = Gateway().action_properties(row)
    assert props['アクション日'] == '2026-10-17T10:15:00.000+09:00'
    assert props['履歴メモ'] == '本文\n署名\n'
    assert row == before


@pytest.mark.parametrize('existing', [True, False])
def test_recheck_preserves_registered_selection_and_unknown_attempt(existing):
    row, _ = sample()
    row.update(company_id='company', contact_id='person', target_id='chosen-facility',
               project_id='chosen-project', channel='dashboard', attempted_properties={'saved': 'value'})
    if existing:
        row['action_id'] = 'existing-action'
    repo, gateway = Mock(), Mock()
    repo.finish.side_effect = lambda row, **changes: {**row, **changes}
    result = Service(repo, gateway).evaluate(row)
    assert result['state'] == ('approved' if existing else 'unknown')
    for key in ('company_id', 'contact_id', 'target_id', 'project_id', 'channel', 'attempted_properties'):
        assert result[key] == row[key]
    gateway.contacts.assert_not_called()
    gateway.validate.assert_not_called()
