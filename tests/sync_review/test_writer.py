from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.db_schema.base import Tool
from src.db_schema.registry import get_schema
from src.sync_engine.id_mapping import IdMapping
from src.sync_review.writer import ApprovedFieldWriter
from src.sync_review.domain import ReviewConflict
from src.sync_review.choice_storage import companion, VIRTUAL_CONTROLLER_FIELD
from src.sync_engine.decided_choices import merge_choice_memo


def test_zoho_controller_delete_preserves_body_and_checks_saved_choices():
    prop = get_schema('project').get_property('サイトコントローラー')
    mapping = IdMapping(notion_key='page', db_key='project', zoho_id='id')
    memo = merge_choice_memo('手書き本文', prop.name, ['リンカーン', 'ねっぱん'], limit=2000)
    expected = {'id': 'id', 'field': 'field20', 'value': 'リンカーン',
                'companion': companion(Tool.ZOHO, 'project', prop.name, {'field70': memo})}
    child = Mock(spec=['get_record', '_client', '_module', '_enabled'])
    child._enabled = True
    child._module = 'Deals'
    child._client = Mock()
    child.get_record.return_value = {'field20': 'リンカーン', 'field70': memo, 'Modified_Time': 'v1'}
    writer = ApprovedFieldWriter(SimpleNamespace(_targets={Tool.ZOHO: child}))
    writer.write(mapping, prop, Tool.ZOHO, expected, None, desired_companion='')
    child._client.update_record.assert_called_once_with('Deals', 'id', {'field20': None, 'field70': '手書き本文'}, expected_version='v1')
    child._client.reset_mock()
    changed = merge_choice_memo(memo, prop.name, ['リンカーン', 'ねっぱん', '手間いらず'], limit=2000)
    child.get_record.return_value['field70'] = changed
    with pytest.raises(ReviewConflict):
        writer.write(mapping, prop, Tool.ZOHO, expected, None, desired_companion='')
    child._client.update_record.assert_not_called()


def test_kintone_memo_only_controller_delete_keeps_other_choice_block():
    prop = get_schema('project').get_property('サイトコントローラー')
    mapping = IdMapping(notion_key='page', db_key='project', kintone_id='id')
    memo = merge_choice_memo('本文', 'ファーストタッチ', ['テレアポ'], limit=65535)
    before = memo
    memo = merge_choice_memo(memo, prop.name, ['ねっぱん'], limit=65535)
    expected = {'id': 'id', 'field': VIRTUAL_CONTROLLER_FIELD, 'value': ['ねっぱん'],
                'companion': companion(Tool.KINTONE, 'project', prop.name, {'文字列__複数行_': memo})}
    child = Mock(spec=['get_record', '_client', '_app'])
    child._client = Mock()
    child._app = 'app'
    child.get_record.return_value = {'文字列__複数行_': memo, '$revision': '3'}
    ApprovedFieldWriter(SimpleNamespace(_targets={Tool.KINTONE: child})).write(
        mapping, prop, Tool.KINTONE, expected, [], desired_companion='')
    child._client.update_record.assert_called_once_with('app', 'id', {'文字列__複数行_': before}, expected_version='3')


def test_sheet_value_changed_after_preview_is_not_overwritten():
    target = Mock(spec=['get_record_by_sync_key'])
    target.get_record_by_sync_key.return_value = {'取引先名': '新しい値'}
    dispatcher = SimpleNamespace(_targets={Tool.SPREADSHEET: target}, _write_spreadsheet_value=Mock())
    with pytest.raises(ReviewConflict):
        ApprovedFieldWriter(dispatcher).write(IdMapping(notion_key='p', db_key='client_master'),
            get_schema('client_master').get_property('取引先名'), Tool.SPREADSHEET,
            {'field': '取引先名', 'value': '元値'}, '')
    dispatcher._write_spreadsheet_value.assert_not_called()


def test_memo_body_delete_preserves_controller_storage():
    memo = merge_choice_memo('本文', 'サイトコントローラー', ['ねっぱん'], limit=2000)
    writer = ApprovedFieldWriter(None)
    plan = writer.plan({'state': 'approved', 'progress': {}, 'dbKey': 'project'},
                      {'zoho': {'supported': True, 'value': memo}},
                      get_schema('project').get_property('メモ'))
    assert plan['zoho'] == merge_choice_memo('', 'サイトコントローラー', ['ねっぱん'], limit=2000)


def test_memo_body_restore_preserves_controller_storage():
    memo = merge_choice_memo('', 'サイトコントローラー', ['リンカーン', 'ねっぱん'], limit=2000)
    writer = ApprovedFieldWriter(SimpleNamespace(_targets={Tool.ZOHO: SimpleNamespace(
        _to_zoho_payload=lambda properties, db_key: {'field70': properties['メモ']})}))
    row = {'state': 'restore_requested', 'progress': {'_restore': {'value': '復元する本文'}},
           'sourceTool': 'zoho', 'dbKey': 'project'}
    plan = writer.plan(row, {'zoho': {'supported': True, 'value': memo}},
                       get_schema('project').get_property('メモ'))
    assert plan['zoho'] == merge_choice_memo('復元する本文', 'サイトコントローラー', ['リンカーン', 'ねっぱん'], limit=2000)
