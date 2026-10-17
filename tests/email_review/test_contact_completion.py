"""所属・メール・姓名補完が別人や過去アドレスを採用しないことを検証。"""
from copy import deepcopy
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from src.email_review.contact_completion import name_parts, contact_changes, unique_named_contact
from src.email_review.domain import Conflict
from src.email_review.gateway import Gateway
from src.email_review.repository import public


@pytest.mark.parametrize('source,expected', [
    ({'名前': '田中 太郎'}, {'姓': '田中', '名': '太郎'}),
    ({'名前': '田中太郎'}, {'姓': '田中太郎'}),
    ({'名前': 'John Smith'}, {'姓': 'John Smith'}),
    ({'名前': '田中'}, {'姓': '田中'}),
    ({'名前': ''}, {}),
    ({'名前': '田中 太郎', '姓': '田中'}, {'名': '太郎'}),
    ({'名前': '田中 太郎', '姓': '別姓'}, {}),
    ({'名前': '田中太郎', '名': '太郎'}, {}),
])
def test_name_completion_preserves_existing_values(source, expected):
    assert name_parts(source) == expected


def test_old_email_is_preserved_with_existing_memo_and_not_restored():
    old = {'名前': '田中 太郎', 'メールアドレス': 'old@example.test', '備考': '既存の連絡事項', '取引先マスター': ['company']}
    patch = contact_changes(old, email='new@example.test', company_id='company', replace_email=True)
    assert patch['備考'] == '既存の連絡事項\n過去メールアドレス: old@example.test'
    updated = {**old, **patch}
    assert contact_changes(updated, email='new@example.test', company_id='company', replace_email=True) == {}
    with pytest.raises(ValueError): contact_changes(updated, email='old@example.test', company_id='company', replace_email=True)
    with pytest.raises(ValueError): contact_changes(old, email='new@example.test', company_id='other', replace_email=True)


def test_same_surname_within_company_is_ambiguous():
    contacts = [{'id': 'one', '名前': '田中 太郎'}, {'id': 'two', '名前': '田中 次郎'}]
    assert unique_named_contact(contacts, ['田中様']) is None
    assert unique_named_contact(contacts, ['田中 太郎様'])['id'] == 'one'


def setup_gateway(monkeypatch, *, email_match=False):
    g = Gateway()
    person = {'id': 'person', '名前': '田中 太郎', 'メールアドレス': 'new@example.test' if email_match else 'old@example.test',
              '取引先マスター': [] if email_match else ['company'], '備考': '既存メモ', '_last_edited_time': '2026-10-01T00:00:00Z'}
    people = [person]
    monkeypatch.setattr(g, 'live', lambda key, pid: deepcopy(person) if key == 'contact' else {'取引先名': '試験株式会社'})
    monkeypatch.setattr(g, 'company_contacts', lambda cid: people)
    monkeypatch.setattr(g, 'contacts', lambda addresses: [{'id': c['id']} for c in people if c['メールアドレス'] == addresses[0]])
    source = {'message': {'internal_date_ms': '1791244800000'}}
    plan = g.plan_completion(source, [{'id': 'company', 'name': '試験株式会社'}], ['田中様'], 'new@example.test')
    assert plan and plan["state"] == "approved"
    plan.update(source)
    return g, person, people, plan


@pytest.mark.parametrize('email_match', [True, False])
def test_plan_is_read_only_and_write_revalidates_then_is_idempotent(monkeypatch, email_match):
    g, person, people, row = setup_gateway(monkeypatch, email_match=email_match)
    before = deepcopy(person)
    row.update(direction='outbound', participants=[{'email': 'new@example.test'}], channel='automatic')
    g.validate(row, row)
    assert person == before
    monkeypatch.setattr('src.email_review.gateway.record_lock', lambda *args: nullcontext())
    client = Mock()
    client.update_page.side_effect = lambda pid, patch: person.update(patch)
    monkeypatch.setattr(g, 'client', lambda key: client)
    changes, owned = g.link_contact(row)
    assert person['取引先マスター'] == ['company']
    assert person['姓'] == '田中' and person['名'] == '太郎'
    assert person['メールアドレス'] == 'new@example.test'
    if not email_match: assert '過去メールアドレス: old@example.test' in person['備考']
    assert client.update_page.call_count == 1
    g.link_contact(row)
    assert client.update_page.call_count == 1
    assert 'contact_completion' not in public(row)


@pytest.mark.parametrize('change', ['memo', 'email', 'company', 'duplicate_email', 'duplicate_name'])
def test_changed_or_duplicate_contact_stops_before_write(monkeypatch, change):
    g, person, people, row = setup_gateway(monkeypatch)
    if change == 'memo': person['備考'] += '追記'
    if change == 'email': person['メールアドレス'] = 'other@example.test'
    if change == 'company': person['取引先マスター'] = ['other']
    if change == 'duplicate_email': people.append({**person, 'id': 'second', 'メールアドレス': 'new@example.test'})
    if change == 'duplicate_name': people.append({**person, 'id': 'second', 'メールアドレス': 'second@example.test'})
    with pytest.raises(Conflict): g.validate_completion(row, row)


def test_old_backlog_cannot_replace_manually_updated_address(monkeypatch):
    g, person, people, row = setup_gateway(monkeypatch)
    person['_last_edited_time'] = '2026-10-17T00:00:00Z'
    with pytest.raises(Conflict, match='更新前'):
        g.validate_completion(row, row)
    result = g.plan_completion(row, [{'id': 'company', 'name': '試験株式会社'}], ['田中様'], 'new@example.test')
    assert result['state'] == 'pending' and '更新前' in result['reason']


def test_unknown_surname_does_not_hide_another_possible_person():
    assert unique_named_contact([{'id': 'one', '名前': '田中 太郎'}, {'id': 'two', '名前': '田中次郎'}], ['田中様']) is None
    assert unique_named_contact([{'id': 'one', '名前': 'John Smith'}], ['John様']) is None


def test_multiple_salutations_do_not_select_only_known_person():
    assert unique_named_contact([{'id': 'one', '名前': '田中 太郎'}], ['田中様', '佐藤様']) is None


def test_rich_memo_and_same_minute_edit_are_held(monkeypatch):
    g, person, people, row = setup_gateway(monkeypatch)
    person['_memo_plain'] = False
    with pytest.raises(Conflict, match='書式'): g.validate_completion(row, row)
    person['_memo_plain'] = True
    from datetime import datetime, timezone
    sent = int(row['message']['internal_date_ms']) / 1000
    person['_last_edited_time'] = datetime.fromtimestamp(sent, timezone.utc).isoformat()
    with pytest.raises(Conflict, match='更新前'): g.validate_completion(row, row)
    person['_last_edited_time'] = None
    with pytest.raises(Conflict, match='更新前'): g.validate_completion(row, row)


def test_completed_enrichment_does_not_overwrite_later_memo(monkeypatch):
    g, person, people, row = setup_gateway(monkeypatch)
    person.update(row['contact_completion']['changes'])
    person['備考'] += '\n本人の後日追記'
    row.update(direction='outbound', participants=[{'email': 'new@example.test'}],
               channel='automatic', contact_completion_applied=True,
               candidates=[{'id': 'company', 'name': '試験株式会社'}])
    monkeypatch.setattr('src.email_review.gateway.record_lock', lambda *args: nullcontext())
    client = Mock()
    monkeypatch.setattr(g, 'client', lambda key: client)
    changed, _ = g.link_contact(row)
    assert changed['備考'].endswith('本人の後日追記')
    client.update_page.assert_not_called()


def test_company_candidates_do_not_fetch_each_page(monkeypatch):
    g = Gateway()
    client = Mock()
    client.query_all_pages.return_value = [{'id': str(i), 'properties': {'名前': {'type': 'title', 'title': [{'plain_text': '田中 太郎'}]}}} for i in range(100)]
    monkeypatch.setattr(g, 'client', lambda key: client)
    live = Mock(side_effect=AssertionError('候補全件を個別取得しない'))
    monkeypatch.setattr(g, 'live', live)
    assert len(g.company_contacts('company')) == 100
    live.assert_not_called()


def test_extended_kanji_name_is_split():
    assert name_parts({'名前': '𠮷田 太郎'}) == {'姓': '𠮷田', '名': '太郎'}
