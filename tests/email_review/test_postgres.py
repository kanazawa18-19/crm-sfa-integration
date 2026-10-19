"""明示した使い捨てローカルDBで実SQL・予約・所有権を検証する。"""
import os
from pathlib import Path
from urllib.parse import urlparse, quote
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor

import psycopg
from psycopg import sql
import pytest
from src.email_review.repository import Repository
from src.email_review.domain import Forbidden, Conflict


@pytest.fixture
def repo(monkeypatch):
    dsn = os.environ.get('EMAIL_REVIEW_TEST_DATABASE_URL')
    if not dsn:
        pytest.skip('使い捨てローカルDBを明示した場合のみ')
    assert urlparse(dsn).hostname in {'127.0.0.1', 'localhost'}
    assert urlparse(dsn).path == '/crm_email_review_test'
    schema = 'email_' + uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    scoped = dsn + '?options=' + quote('-csearch_path=' + schema)
    monkeypatch.setenv('DATABASE_URL', scoped)
    from psycopg.rows import dict_row
    monkeypatch.setattr('src.email_review.repository._connect', lambda: psycopg.connect(scoped, row_factory=dict_row))
    try:
        with psycopg.connect(scoped) as conn:
            conn.execute('CREATE TABLE "User" (id TEXT PRIMARY KEY,email TEXT NOT NULL)')
            conn.execute('CREATE TABLE "ClientNameIndex" ("notionPageId" TEXT PRIMARY KEY,"rawName" TEXT,"normalizedName" TEXT)')
            conn.execute("INSERT INTO \"User\" VALUES ('one','one@example.test'),('two','two@example.test')")
            conn.execute(Path('dashboard/prisma/migrations/20261016090000_email_review/migration.sql').read_text())
        yield Repository()
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def row(id='a', canonical=None):
    return {'id': id, 'canonical_key': canonical, 'message': {'label_ids': ['SENT'], 'body_text': '本文'}, 'state': 'new', 'revision': 0}


def test_upgrade_and_cross_owner_share_one_claim(repo):
    repo.capture({**row(), 'message': {'label_ids': ['SENT'], 'body_text': ''}}, 'one@example.test', 'gmail1', 'thread')
    upgraded = {**row('rfc', 'rfc'), 'message': {'label_ids': ['SENT'], 'body_text': '本文'}}
    repo.capture(upgraded, 'one@example.test', 'gmail1', 'thread')
    repo.capture(upgraded, 'two@example.test', 'gmail2', 'other-thread')
    assert repo.get('a', 'one')['canonical_key'] == 'rfc'
    assert repo.get('a', 'two')['canonical_key'] == 'rfc'
    assert repo.claim('new', 'evaluating')['id'] == 'a'
    assert repo.claim('new', 'evaluating') is None


def test_already_canonical_merges_older_unidentified_capture(repo):
    repo.capture(row(), 'one@example.test', 'gmail1', 'thread')
    repo.capture(row('rfc', 'rfc'), 'two@example.test', 'gmail2', 'other')
    repo.capture(row('rfc', 'rfc'), 'one@example.test', 'gmail1', 'thread')
    assert repo.get('rfc', 'one')['canonical_key'] == 'rfc'
    assert repo.claim('new', 'evaluating')['id'] == 'rfc'
    assert repo.claim('new', 'evaluating') is None


def test_owner_and_parallel_decision_revision(repo):
    repo.capture(row(), 'one@example.test', 'gmail1', 'thread')
    with pytest.raises(Forbidden):
        repo.get('a', 'two')
    def decide():
        try:
            return repo.decision('a', 'one', 0, 'hold', {}, 'dashboard')['state']
        except Conflict:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: decide(), range(2))) == ['conflict', 'held']


def test_parallel_capture_and_claim(repo):
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: repo.capture(row('rfc', 'rfc'), 'one@example.test', 'gmail1', 'thread'), range(3)))
        results = list(pool.map(lambda _: repo.claim('new', 'evaluating'), range(3)))
    assert sum(r is not None for r in results) == 1


def test_same_rfc_with_different_content_does_not_grant_other_owner_access(repo):
    original = {**row('same', 'same'), 'message': {'label_ids': ['SENT'], 'body_text': '本人だけの本文', 'subject': '件名'}}
    repo.capture(original, 'one@example.test', 'g1', 'thread')
    forged = {**original, 'message': {'label_ids': ['SENT'], 'body_text': '別の本文', 'subject': '件名'}}
    with pytest.raises(Conflict):
        repo.capture(forged, 'two@example.test', 'g2', 'thread2')
    with pytest.raises(Forbidden):
        repo.get('same', 'two')


def test_decision_response_filters_private_thread_evidence(repo):
    shared = {**row('shared', 'shared'), 'state': 'pending', 'evidence': [
        {'review_id': 'private', 'value': '非共有の根拠'}, {'review_id': 'shared', 'value': '共有の根拠'}],
        'candidates': [{'id': 'company', 'evidence': [{'review_id': 'private', 'value': '非共有の根拠'}]}]}
    repo.capture(shared, 'one@example.test', 'g1', 'thread')
    repo.capture(shared, 'two@example.test', 'g2', 'other')
    repo.capture(row('private'), 'one@example.test', 'private', 'thread')
    result = repo.decision('shared', 'two', repo.get('shared', 'two')['revision'], 'hold', {}, 'dashboard')
    assert [e['value'] for e in result['evidence']] == ['共有の根拠']
    assert result['candidates'][0]['evidence'] == []


def test_real_journal_to_automatic_actions_without_duplicate_replay(repo, monkeypatch):
    from src.email_review.service import Service
    from tests.email_review.test_service import Gateway
    monkeypatch.setenv('EMAIL_REVIEW_ENABLED', 'true')
    company = '11111111-1111-1111-1111-111111111111'
    person = '22222222-2222-2222-2222-222222222222'
    from src.email_review.repository import _connect
    with _connect() as conn:
        conn.execute('INSERT INTO "ClientNameIndex" VALUES (%s,%s,%s)', (company, '試験株式会社', '試験'))
    common = {'owner': 'one@example.test', 'thread_id': 'thread', 'participants': [{'email': 'contact@outside.test', 'name': '試験担当'}], 'subject': '試験', 'delivery': 'not_started'}
    outgoing = {**row('sent', 'sent'), **common, 'direction': 'outbound', 'message': {'label_ids': ['SENT'], 'id': 's', 'body_text': '試験株式会社\n担当様\n本文'}}
    incoming = {**row('received', 'received'), **common, 'direction': 'inbound', 'message': {'label_ids': ['SENT'], 'id': 'r', 'body_text': '返信本文\n---\n試験株式会社\ncontact@outside.test'}}
    repo.capture(outgoing, 'one@example.test', 's', 'thread')
    repo.capture(incoming, 'one@example.test', 'r', 'thread')
    gateway = Gateway()
    gateway.contacts = lambda addresses: [{'id': person, 'email': 'contact@outside.test', 'name': '試験担当', 'affiliations': [company]}]
    def create(props):
        gateway.created += 1
        return f'action-{gateway.created}'
    gateway.create_action = create
    service = Service(repo, gateway)
    assert service.drain()['processed'] == 1
    assert gateway.created == 1
    assert repo.get('sent', 'one')['delivery'] == 'delivered'
    assert repo.get('received', 'one')['state'] == 'excluded'
    repo.capture(outgoing, 'one@example.test', 's', 'thread')
    assert service.drain()['processed'] == 0
    assert gateway.created == 1


def test_missing_body_cannot_grant_shared_access_by_snippet(repo):
    original = {**row('same', 'same'), 'message': {'label_ids': ['SENT'], 'body_text': '本人だけの本文', 'snippet': '同じ先頭'}}
    repo.capture(original, 'one@example.test', 'g1', 'thread')
    incomplete = {**original, 'message': {'label_ids': ['SENT'], 'body_text': '', 'snippet': '同じ先頭'}}
    with pytest.raises(Conflict):
        repo.capture(incomplete, 'two@example.test', 'g2', 'thread2')
    with pytest.raises(Forbidden):
        repo.get('same', 'two')


def test_persisted_email_match_rechecks_affiliation_before_creation(repo, monkeypatch):
    from unittest.mock import Mock
    from src.email_review.gateway import Gateway
    from src.email_review.service import Service
    company = '11111111-1111-1111-1111-111111111111'
    person = '22222222-2222-2222-2222-222222222222'
    saved = {'message': {'label_ids': ['SENT']}, **row('email-match', 'email-match'), 'direction': 'outbound',
             'participants': [{'email': 'person@example.test'}]}
    repo.capture(saved, 'one@example.test', 'gmail-match', 'thread')
    gateway = Gateway()
    contacts = [{'id': person, 'email': 'person@example.test', 'affiliations': [company]}]
    monkeypatch.setattr(gateway, 'contacts', lambda addresses: contacts)
    monkeypatch.setattr(gateway, 'live', lambda key, pid: {'名前': '試験担当', 'メールアドレス': 'person@example.test',
        '取引先マスター': [company]} if key == 'contact' else {'取引先名': '試験会社'})
    monkeypatch.setattr(gateway, 'create_action', Mock())
    service = Service(repo, gateway)
    service.evaluate(repo.claim('new', 'evaluating'))
    restored = repo.claim('approved', 'processing')
    assert restored['match_basis'] == 'contact_email'
    assert restored['channel'] == 'automatic'
    contacts[0]['affiliations'] = []
    result = service.process(restored)
    assert result['state'] == 'pending'
    assert '所属が変わって' in result['reason']
    gateway.create_action.assert_not_called()


@pytest.mark.parametrize('mode', ['email_change', 'company_fill', 'duplicate_name', 'duplicate_company'])
def test_contact_completion_through_persisted_service(repo, monkeypatch, mode):
    from copy import deepcopy
    from contextlib import nullcontext
    from unittest.mock import Mock
    from src.email_review.gateway import Gateway
    from src.email_review.service import Service
    company = '11111111-1111-1111-1111-111111111111'
    person = {'id': '22222222-2222-2222-2222-222222222222', '名前': '田中 太郎',
              'メールアドレス': 'new@example.test' if mode == 'company_fill' else 'old@example.test',
              '取引先マスター': [] if mode == 'company_fill' else [company],
              '備考': '既存メモ', '_last_edited_time': '2026-10-01T00:00:00Z'}
    people = [person]
    if mode == 'duplicate_name': people.append({**person, 'id': '33333333-3333-3333-3333-333333333333'})
    masters = [{'id': company, 'name': '試験株式会社'}]
    if mode == 'duplicate_company': masters.append({'id': 'other', 'name': '試験株式会社'})
    monkeypatch.setattr(repo, 'masters', lambda values: masters)
    source = {**row('completion', 'completion'), 'direction': 'outbound', 'owner': 'one@example.test',
              'participants': [{'email': 'new@example.test'}], 'message': {'label_ids': ['SENT'], 'id': 'completion',
              'body_text': '試験株式会社\n田中様、\n本文', 'internal_date_ms': '1791244800000'}}
    repo.capture(source, 'one@example.test', 'gmail-completion', 'thread')
    gateway = Gateway()
    monkeypatch.setattr(gateway, 'live', lambda key, pid: deepcopy(person) if key == 'contact' else {'取引先名': '試験株式会社'})
    monkeypatch.setattr(gateway, 'contacts', lambda addresses: [{'id': p['id'], 'email': p['メールアドレス'], 'affiliations': p['取引先マスター']} for p in people if p['メールアドレス'] == addresses[0]])
    monkeypatch.setattr(gateway, 'company_contacts', lambda cid: people)
    monkeypatch.setattr('src.email_review.gateway.record_lock', lambda *args: nullcontext())
    client = Mock()
    client.update_page.side_effect = lambda pid, patch: person.update(patch)
    monkeypatch.setattr(gateway, 'client', lambda key: client)
    monkeypatch.setattr(gateway, 'actor', lambda owner: 'actor')
    create = Mock(return_value='action')
    monkeypatch.setattr(gateway, 'create_action', create)
    monkeypatch.setattr(gateway, 'dispatch', lambda *args: 'delivered')
    service = Service(repo, gateway)
    evaluated = service.evaluate(repo.claim('new', 'evaluating'))
    if mode.startswith('duplicate'):
        assert evaluated['state'] == 'pending'
        client.update_page.assert_not_called()
        create.assert_not_called()
        return
    assert evaluated['state'] == 'approved'
    pending = repo.claim('approved', 'processing')
    assert pending['match_basis'] == 'contact_completion'
    assert pending['contact_completion']['expected']['備考'] == '既存メモ'
    done = service.process(pending)
    assert done['state'] == 'done' and done['delivery'] == 'delivered'
    assert person['メールアドレス'] == 'new@example.test'
    assert person['取引先マスター'] == [company]
    assert person['姓'] == '田中' and person['名'] == '太郎'
    if mode == 'email_change': assert person['備考'] == '既存メモ\n過去メールアドレス: old@example.test'
    create.assert_called_once()
