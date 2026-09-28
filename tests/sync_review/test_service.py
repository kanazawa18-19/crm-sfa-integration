from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from src.sync_engine.sync_event import SyncEvent
from src.sync_review.domain import ReviewConflict
from src.sync_review.service import FieldReviewService


class MemoryJournal:
    def __init__(self):
        self.rows = {}
    def active_for_record(self, *args):
        return {r['propertyName']: r for r in self.rows.values() if r['state'] not in {'done', 'superseded'}}
    def enqueue(self, **data):
        key = str(len(self.rows))
        row = dict(id=key, state='pending', dbKey=data['db_key'], notionKey=data['notion_key'],
                   propertyName=data['property_name'], sourceTool=data['source_tool'],
                   snapshot=data['snapshot'], progress={})
        self.rows[key] = row
        return row
    def get(self, key):
        return self.rows[key]
    def finish(self, key, state, **kwargs):
        self.rows[key]['state'] = state
    def record_progress(self, key, tool, value):
        self.rows[key]['progress'][tool] = value


@pytest.fixture
def setup():
    mapping = IdMapping(notion_key='page', db_key='client_master', zoho_id='zoho')
    store = SQLiteIdMappingStore(':memory:')
    store.upsert(mapping)
    journal = MemoryJournal()
    records = {Tool.NOTION: {'取引先名': ''}, Tool.ZOHO: {'Account_Name': '元会社'}}
    targets = {}
    for tool in records:
        target = Mock(spec=['get_record'])
        target.get_record.side_effect = lambda *a, tool=tool, **k: records[tool]
        targets[tool] = target
    service = FieldReviewService(journal, targets)
    prop = get_schema('client_master').get_property('取引先名')
    event = SyncEvent(source_tool=Tool.NOTION, db_key='client_master', external_id='page',
                      occurred_at=datetime.now(timezone.utc), properties={'取引先名': ''})
    return service, journal, records, store, mapping, prop, event


def test_delete_held_while_other_property_continues(setup):
    service, journal, records, store, mapping, prop, event = setup
    phone = get_schema('client_master').get_property('電話番号')
    allowed, held = service.filter_properties(event, mapping, [(prop.name, prop, ''), (phone.name, phone, '123')])
    assert held == [prop.name]
    assert allowed == [(phone.name, phone, '123')]
    assert journal.rows['0']['snapshot']['zoho']['value'] == '元会社'
    assert records[Tool.ZOHO]['Account_Name'] == '元会社'


def approve(setup):
    service, journal, records, store, mapping, prop, event = setup
    service.filter_properties(event, mapping, [(prop.name, prop, '')])
    journal.rows['0']['state'] = 'approved'
    return service, journal, records, store


def test_unapproved_never_writes(setup):
    service, journal, records, store, mapping, prop, event = setup
    service.filter_properties(event, mapping, [(prop.name, prop, '')])
    writer = Mock()
    with pytest.raises(ReviewConflict):
        service.execute('0', store=store, writer=writer)
    writer.write.assert_not_called()


def test_changed_snapshot_never_writes(setup):
    service, journal, records, store = approve(setup)
    records[Tool.ZOHO]['Account_Name'] = '利用者が変更'
    writer = Mock()
    writer.plan.return_value = {'zoho': None}
    writer.companion_plan.return_value = {}
    with pytest.raises(ReviewConflict):
        service.execute('0', store=store, writer=writer)
    writer.write.assert_not_called()
    assert journal.rows['0']['state'] == 'failed'


def test_partial_delivery_restarts_only_remaining_and_verifies_real_values(setup):
    service, journal, records, store = approve(setup)
    journal.rows['0']['progress']['zoho'] = {'state': 'writing', 'desired': None}
    records[Tool.ZOHO]['Account_Name'] = None
    writer = Mock()
    writer.plan.return_value = {'notion': '', 'zoho': None}
    writer.companion_plan.return_value = {}
    result = service.execute('0', store=store, writer=writer)
    writer.write.assert_not_called()
    assert result['state'] == 'done'


def test_done_is_idempotent(setup):
    service, journal, records, store = approve(setup)
    journal.rows['0']['state'] = 'done'
    writer = Mock()
    service.execute('0', store=store, writer=writer)
    writer.plan.assert_not_called()


def test_keep_blank_blocks_reverse_sync_until_source_edited(setup):
    service, journal, records, store, mapping, prop, event = setup
    service.filter_properties(event, mapping, [(prop.name, prop, '')])
    journal.rows['0']['state'] = 'kept_blank'
    allowed, held = service.filter_properties(event, mapping, [(prop.name, prop, '')])
    assert allowed == []
    records[Tool.NOTION][prop.name] = '本人が再入力'
    allowed, held = service.filter_properties(event, mapping, [(prop.name, prop, '本人が再入力')])
    assert len(allowed) == 1
    assert journal.rows['0']['state'] == 'superseded'


def test_kintone_initial_blank_is_not_a_delete_but_known_transition_is(setup):
    from dataclasses import replace
    service, journal, records, store, mapping, prop, event = setup
    event = replace(event, source_tool=Tool.KINTONE)
    journal.source_values = Mock(return_value={})
    journal.observe_source = Mock()
    _, held = service.filter_properties(event, mapping, [(prop.name, prop, '')])
    assert held == [] and journal.rows == {}
    journal.source_values.return_value = {prop.name: '以前の会社'}
    _, held = service.filter_properties(event, mapping, [(prop.name, prop, '')])
    assert held == [prop.name]
    journal.observe_source.assert_called()


def test_corrupt_companion_isolated_to_its_tool():
    from src.sync_engine.decided_choices import merge_choice_memo
    mapping = IdMapping(notion_key='page', db_key='project', zoho_id='zoho')
    prop = get_schema('project').get_property('サイトコントローラー')
    target = Mock(spec=['get_record'])
    target.get_record.return_value = {'field20': 'リンカーン', 'field70': 123}
    service = FieldReviewService(MemoryJournal(), {Tool.ZOHO: target})
    snapshot = service.snapshot(mapping, prop)
    assert snapshot['zoho']['supported'] is False
    target.get_record.return_value = {'field20': 'リンカーン', 'field70': merge_choice_memo('', prop.name, ['リンカーン', 'ねっぱん'], limit=2000)}
    snapshot = service.snapshot(mapping, prop)
    assert snapshot['zoho']['canonical'] == ['リンカーン', 'ねっぱん']


@pytest.mark.parametrize('tool', [Tool.NOTION, Tool.ZOHO, Tool.KINTONE, Tool.SPREADSHEET])
def test_other_tool_snapshot_does_not_prove_delete_intent(setup, tool):
    from dataclasses import replace
    service, journal, records, store, mapping, prop, event = setup
    journal.source_values = Mock(return_value={prop.name: '以前の値'})
    event = replace(event, source_tool=tool)
    assert service.filter_properties(event, mapping, [(prop.name, prop, '')], observe_source=False) == ([], [])
    assert journal.rows == {}


def test_owner_blank_does_not_create_unexecutable_delete_review():
    journal = MemoryJournal()
    service = FieldReviewService(journal, {})
    mapping = IdMapping('page', 'project', zoho_id='z')
    prop = get_schema('project').get_property('担当メンバー')
    event = SyncEvent(Tool.NOTION, 'project', 'page', datetime.now(timezone.utc), properties={prop.name: []})
    allowed, held = service.filter_properties(event, mapping, [(prop.name, prop, [])])
    assert allowed == [] and held == [prop.name]
    assert journal.rows == {}
