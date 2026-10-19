"""メール本体・参照元を保持した移管と、中断・第三者変更を隔離DBで確認する。"""
from contextlib import nullcontext

import pytest

from src.record_merge import email_history as emails
from src.record_merge.application import MergeService, validate_plan
from src.record_merge.domain import MergeHeld
from tests.record_merge.test_postgres import local


@pytest.fixture
def history(local):
    with emails.connect() as conn:
        conn.execute('''CREATE TABLE "EmailLog" (
            id text PRIMARY KEY, "contactPageId" text NOT NULL, "gmailMessageId" text UNIQUE,
            "contactEmail" text, "repEmail" text, "gmailThreadId" text, direction text,
            subject text, snippet text, "sentAt" timestamptz, "createdAt" timestamptz,
            "incidentScore" int, "incidentPriority" text, "digestedAt" timestamptz)''')
        conn.execute('''CREATE TABLE "EmailReminderLog" (id text, "emailLogId" text REFERENCES "EmailLog"(id))''')
        for identifier, owner, date in [('a', 'source', '2026-10-19'), ('b', 'source', '2026-10-17'),
                                        ('c', 'target', '2026-10-18'), ('d', 'other', '2026-10-20')]:
            conn.execute('''INSERT INTO "EmailLog" VALUES
                (%s,%s,%s,'customer@example.test','rep@example.test','thread','inbound',
                 '件名','本文',%s,'2026-10-19',3,'medium',NULL)''', (identifier, owner, identifier, date + "T00:00:00+00:00"))
        conn.execute('''INSERT INTO "EmailReminderLog" VALUES ('reminder','a')''')
    return emails.make_step('source', 'target', emails.snapshot('source', 'target'))


def rows():
    with emails.connect() as conn:
        return conn.execute('SELECT * FROM "EmailLog" ORDER BY id').fetchall()


def test_Notion表示のみ実保存精度へ揃えメール本体の秒を残す():
    value = '2026-10-02T06:39:39+00:00'
    assert emails.notion_date(value) == '2026-10-02T06:39:00+00:00'
    assert emails.latest_date(value) == value


def test_参照先だけを移管し再開は冪等で通知状態を保持(history):
    before = rows()
    with emails.connect() as conn:
        conn.execute('UPDATE "EmailLog" SET "digestedAt"=now() WHERE id=\'a\'')
    emails.write(history)
    emails.write(history)
    after = rows()
    for old, new in zip(before, after):
        assert {k: v for k, v in old.items() if k not in {'contactPageId', 'digestedAt'}} == {
            k: v for k, v in new.items() if k not in {'contactPageId', 'digestedAt'}}
        assert new['contactPageId'] == ('target' if old['contactPageId'] == 'source' else old['contactPageId'])
    assert after[0]['digestedAt'] is not None
    with emails.connect() as conn:
        assert conn.execute('SELECT "emailLogId" FROM "EmailReminderLog"').fetchone()['emailLogId'] == 'a'
    assert emails.read(history) == history['desired']


@pytest.mark.parametrize('change', ['added', 'changed', 'deleted', 'third_party', 'mixed'])
def test_比較後の変更は全件保持して保留(history, change):
    statements = {
        'added': '''INSERT INTO "EmailLog" (id,"contactPageId","sentAt") VALUES ('e','source',now())''',
        'changed': '''UPDATE "EmailLog" SET subject='第三者変更' WHERE id='a' ''',
        'deleted': '''DELETE FROM "EmailLog" WHERE id='b' ''',
        'third_party': '''UPDATE "EmailLog" SET "contactPageId"='other' WHERE id='a' ''',
        'mixed': '''UPDATE "EmailLog" SET "contactPageId"='target' WHERE id='a' ''',
    }
    with emails.connect() as conn:
        conn.execute(statements[change])
    before = rows()
    with pytest.raises(MergeHeld):
        emails.write(history)
    assert rows() == before


def test_更新途中の失敗は全件ロールバック(history):
    with emails.connect() as conn:
        conn.execute('''ALTER TABLE "EmailLog" ADD CONSTRAINT fail_second
            CHECK (NOT(id='b' AND "contactPageId"='target')) NOT VALID''')
    before = rows()
    with pytest.raises(Exception):
        emails.write(history)
    assert rows() == before


def test_移管手順は承認した固定範囲以外や省略を拒否(history):
    snapshot = {'dbKey': 'contact', 'sourceId': 'source', 'targetId': 'target', 'children': [],
                'emailHistory': emails.snapshot('source', 'target')}
    archive = {'kind': 'archive', 'dbKey': 'contact', 'id': 'source', 'before': False, 'desired': True}
    validate_plan(snapshot, [history, archive])
    for bad in ({**history, 'targetId': 'other'}, {**history, 'rows': []}, {**history, 'desired': []}):
        with pytest.raises(MergeHeld):
            validate_plan(snapshot, [bad, archive])
    with pytest.raises(MergeHeld):
        validate_plan(snapshot, [archive])
    # 過去に承認された手順は増やさず、従来どおり検証する。
    validate_plan({k: v for k, v in snapshot.items() if k != 'emailHistory'}, [archive])


@pytest.mark.parametrize('lose_response', [False, True])
def test_統合はメールと最終日時を保持してから元を閉じる(history, lose_response):
    from tests.record_merge.test_gateway import Client, raw
    from tests.record_merge.test_application import Journal
    from src.db_schema.registry import ALL_SCHEMAS
    from src.sync_engine.id_mapping import SQLiteIdMappingStore, IdMapping
    from src.record_merge.notion_gateway import NotionMergeGateway
    clients = {schema.key: Client(schema.key, {}) for schema in ALL_SCHEMAS}
    source = raw('contact', 'source', {'名前': '合成人物', '最終メール日時': '2026-10-16T00:00:00+00:00'})
    target = raw('contact', 'target', {'名前': '合成人物', '最終メール日時': '2026-10-18T00:00:00+00:00'})
    clients['contact'].pages = {'source': source, 'target': target}
    original_update = clients['contact'].update_page
    def update(page, values):
        original_update(page, values)
        if values.get('最終メール日時'):
            target['properties']['最終メール日時']['date']['start'] = '2026-10-19T00:00:00.000Z'
    clients['contact'].update_page = update
    store = SQLiteIdMappingStore(); store.upsert(IdMapping('target', 'contact'))
    gateway = NotionMergeGateway(clients, store)
    snapshot = gateway.snapshot('contact', 'source', 'target')
    steps = gateway.plan(snapshot, {})
    assert steps[-2]['kind'] == 'email_history'
    assert steps[0]['desired']['最終メール日時'] == '2026-10-19T00:00:00+00:00'
    journal = Journal(steps)
    journal.job.update(dbKey='contact', sourceId='source', targetId='target', snapshot=snapshot)
    original_archive = clients['contact'].archive_page
    def archive(identifier):
        assert emails.read(steps[-2]) == steps[-2]['desired']
        original_archive(identifier)
    clients['contact'].archive_page = archive
    writes = []
    original_write = gateway.write
    def write(step):
        original_write(step)
        if step['kind'] == 'email_history':
            writes.append(step['kind'])
            if lose_response:
                raise TimeoutError('応答断')
    gateway.write = write
    if lose_response:
        with pytest.raises(MergeHeld):
            MergeService(journal, gateway, lambda _: nullcontext()).execute('op', 'manager')
        assert not source['archived']
    assert MergeService(journal, gateway, lambda _: nullcontext()).execute('op', 'manager')['state'] == 'done'
    assert source['archived']
    assert writes == ['email_history']


def test_移管後の応答断は二重移管せず再開(history):
    emails.write(history)
    assert emails.read(history) == history['desired']
    with emails.connect() as conn:
        conn.execute('''INSERT INTO "EmailLog" (id,"contactPageId","sentAt") VALUES ('e','source',now())''')
    with pytest.raises(MergeHeld):
        emails.read(history)


def test_旧contact計画はメールが残ればアーカイブを保留(history):
    from src.record_merge.notion_gateway import NotionMergeGateway
    gateway = NotionMergeGateway({}, None)
    gateway.verify_identity = lambda _: None
    with pytest.raises(MergeHeld, match='旧計画'):
        gateway.verify_archive_ready({'dbKey': 'contact', 'sourceId': 'source', 'targetId': 'target',
                                      'snapshot': {}, 'steps': []})
