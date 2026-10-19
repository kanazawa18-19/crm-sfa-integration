"""既存受付の証拠補完は実SQLで確認し、完了行を再配送しない。"""
import os
from pathlib import Path
import psycopg
from psycopg.rows import dict_row
import pytest
from tests.record_merge.test_postgres import local
from src.email_review import repository


@pytest.fixture
def repo(local,monkeypatch):
    monkeypatch.setattr(repository,'_connect',lambda:psycopg.connect(os.environ['DATABASE_URL'],row_factory=dict_row))
    with repository._connect() as conn:
        conn.execute('CREATE TABLE "ClientNameIndex" ("notionPageId" text PRIMARY KEY,"rawName" text,"normalizedName" text)')
        conn.execute('ALTER TABLE "User" ADD COLUMN email text')
        conn.execute("UPDATE \"User\" SET email='rep@example.test' WHERE id='manager'")
        conn.execute(Path('dashboard/prisma/migrations/20261016090000_email_review/migration.sql').read_text())
    return repository.Repository()


@pytest.mark.parametrize('state',['held','done'])
def test_同一Gmail実読で本文既存でも証拠補完し完了は維持(repo,state):
    original={'id':'r','canonical_key':'r','owner':'rep@example.test','direction':'outbound',
              'message':{'body_text':'本文','message_id_header':'<synthetic>'},
              'state':state,'revision':0,'delivery':'delivered' if state=='done' else 'not_started',
              'reason':'送信済みの証拠を確認できません。下書き・状態不明のメールは登録しません'}
    if state=='done':original['action_id']='00000000-0000-0000-0000-000000000001'
    repo.capture(original,'rep@example.test','gmail','thread')
    fresh={**original,'message':{**original['message'],'label_ids':['SENT']}}
    repo.capture(fresh,'rep@example.test','gmail','thread')
    result=repo.get('r','manager')
    assert result['state']==('new' if state=='held' else 'done')
    assert result['delivery']==original['delivery'] and result.get('action_id')==original.get('action_id')
    with repository._connect() as conn:
        saved=conn.execute('SELECT data FROM "EmailReview" WHERE id=%s',('r',)).fetchone()['data']
    assert saved['message']['body_text']=='本文' and saved['message']['label_ids']==['SENT']
    assert result['sent_evidence'] is True
    if state=='done':assert not repository.action_registration_blocked('00000000000000000000000000000001')


@pytest.mark.parametrize('labels,blocked',[(None,True),(['DRAFT'],True),(['SENT'],False),(['DRAFT','SENT'],True)])
def test_受付actionIDと送信証拠で通常通知を判定(repo,labels,blocked):
    repo.capture({'id':'r','message':{'label_ids':labels},'action_id':'00000000-0000-0000-0000-000000000001','direction':'outbound',
                  'state':'done','revision':0},'rep@example.test','gmail','thread')
    assert repository.action_registration_blocked('00000000000000000000000000000001') is blocked
    assert not repository.action_registration_blocked('unrelated')


def test_大文字UUID通知でも32桁保存された受付を見落とさない(repo):
    identifier='abcdef01000000000000000000000001'
    repo.capture({'id':'r','message':{},'action_id':identifier,'direction':'outbound',
                  'state':'done','revision':0},'rep@example.test','gmail','thread')
    assert repository.action_registration_blocked('ABCDEF01-0000-0000-0000-000000000001')
