"""親関連だけの既反映判定。原本・対応ID・実物のいずれか不一致なら保留する。"""
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from src.db_schema.base import Tool
from src.email_review import relation_readback as readback
from src.sync_engine.dispatcher import DispatchResult, PropertyDispatchResult
from src.sync_engine.sync_event import SyncEvent


@pytest.fixture
def setup(monkeypatch):
    def build(key='action'):
        name = '取引先マスター' if key == 'contact' else '👨‍👩‍👧‍👦 取引先マスター'
        properties = {name: ['parent'], '履歴メモ': '合成本文'}
        now = datetime(2026, 10, 19, tzinfo=timezone.utc)
        source = {**properties, '_last_edited_time': now.isoformat()}
        gateway = SimpleNamespace(live=lambda *_: source)
        mapping = SimpleNamespace(db_key=key, notion_key='child', zoho_id='z1', kintone_id='k1')
        parent = SimpleNamespace(db_key='client_master', notion_key='parent', zoho_id='z2', kintone_id='k2')
        mappings = {'child': mapping, 'parent': parent}
        records = {
            (Tool.ZOHO, key, 'z1'): {'id': 'z1', 'field25' if key == 'contact' else 'field6': {'id': 'z2'}},
            (Tool.ZOHO, 'client_master', 'z2'): {'id': 'z2'},
            (Tool.KINTONE, key, 'k1'): {'$id': 'k1', 'client_name': '合成取引先'},
            (Tool.KINTONE, 'client_master', 'k2'): {'$id': 'k2', '顧客名': '合成取引先'},
        }
        active = set(); completed = []
        class Guard:
            rejects = lambda self, _: False
            def save_fields(self, names, stamp, *, completed):
                assert active == {(key, 'child'), ('client_master', 'parent')}
                assert completed
                saved.append((names, stamp))
        saved = []
        guard = Guard()
        @contextmanager
        def lock(_store, db_key, identifier):
            active.add((db_key, identifier))
            try: yield guard
            finally: active.remove((db_key, identifier))
        monkeypatch.setattr(readback, 'acquire_record_sync_lock', lock)
        monkeypatch.setattr(readback, 'resolve_alias', lambda *_: None)
        monkeypatch.setattr(readback, 'require_record_available', lambda *_: None)
        def get(tool, identifier, *, db_key):
            assert active == {(key, 'child'), ('client_master', 'parent')}
            return records.get((tool, db_key, identifier))
        targets = {tool: SimpleNamespace(get_record=lambda identifier, db_key, t=tool: get(t, identifier, db_key=db_key))
                   for tool in (Tool.ZOHO, Tool.KINTONE)}
        wiring = SimpleNamespace(id_mapping_store=SimpleNamespace(get=mappings.get),
                                 dispatcher=SimpleNamespace(_dispatcher=SimpleNamespace(_targets=targets)))
        event = SyncEvent(source_tool=Tool.NOTION, db_key=key, external_id='child', occurred_at=now,
                          properties=properties, expected_source_properties=properties)
        skips = {Tool.ZOHO} if key == 'contact' else {Tool.ZOHO, Tool.KINTONE}
        result = DispatchResult(False, properties=(PropertyDispatchResult(name, None, skipped_tools=frozenset(skips)),))
        return SimpleNamespace(gateway=gateway, wiring=wiring, event=event, result=result, records=records,
                               source=source, mappings=mappings, saved=saved, guard=guard, name=name)
    return build


def confirm(case):
    return readback.confirm_parent_relation(case.gateway, case.wiring, case.event, case.result)


@pytest.mark.parametrize('key', ['contact', 'action'])
def test_作成済みの親関連だけ実物一致で完了時刻を進める(setup, key):
    case = setup(key)
    assert confirm(case)
    assert case.saved == [([case.name], case.event.occurred_at)]


@pytest.mark.parametrize('change', ['wrong_lookup', 'wrong_name', 'missing_parent', 'wrong_child', 'mapping_kind', 'source', 'time', 'newer_event', 'alias'])
def test_不一致や変更では完了記録を作らない(setup, monkeypatch, change):
    c = setup()
    if change == 'wrong_lookup': c.records[Tool.ZOHO, 'action', 'z1']['field6'] = {'id': 'other'}
    if change == 'wrong_name': c.records[Tool.KINTONE, 'action', 'k1']['client_name'] = '別取引先'
    if change == 'missing_parent': del c.records[Tool.ZOHO, 'client_master', 'z2']
    if change == 'wrong_child': c.records[Tool.ZOHO, 'action', 'z1']['id'] = 'other'
    if change == 'mapping_kind': c.mappings['parent'].db_key = 'project'
    if change == 'source': c.source['履歴メモ'] = '第三者変更'
    if change == 'time': c.source['_last_edited_time'] = '2026-10-19T00:01:00+00:00'
    if change == 'newer_event': c.guard.rejects = lambda _: True
    if change == 'alias': monkeypatch.setattr(readback, 'resolve_alias', lambda *_: 'canonical')
    assert not confirm(c)
    assert c.saved == []


@pytest.mark.parametrize('change', ['other_property', 'other_tool', 'review', 'registration', 'related', 'skipped', 'multiple'])
def test_他の保留理由を配送済みに読み替えない(setup, change):
    c = setup(); prop = c.result.properties[0]
    if change == 'other_property': prop = replace(prop, property_name='履歴メモ')
    if change == 'other_tool': prop = replace(prop, skipped_tools=frozenset({Tool.SPREADSHEET}))
    if change == 'review': prop = replace(prop, review_pending=True)
    if change == 'registration': prop = replace(prop, registration_pending_tools=frozenset({Tool.ZOHO}))
    if change == 'related': prop = replace(prop, related_db_key='project')
    c.result = replace(c.result, properties=(prop,), skipped=change == 'skipped')
    if change == 'multiple': c.event.properties[c.name].append('another')
    assert not confirm(c)
    assert c.saved == []


def test_読み取り中に原本が変わったら完了しない(setup):
    c = setup()
    reads = [0]
    def live(*_):
        reads[0] += 1
        return c.source if reads[0] == 1 else {**c.source, '履歴メモ': '第三者変更'}
    c.gateway.live = live
    assert not confirm(c)
    assert c.saved == []


def test_実物取得失敗はGatewayでpartialへ残る(setup, monkeypatch):
    from src.email_review.gateway import Gateway
    from src.sync_engine import production_wiring
    c = setup()
    c.wiring.dispatcher.dispatch = lambda _: c.result
    monkeypatch.setattr(production_wiring, 'get_production_wiring', lambda: c.wiring)
    gateway = Gateway()
    monkeypatch.setattr(gateway, 'client', lambda _: SimpleNamespace(get_raw_page=lambda _: {'last_edited_time': c.event.occurred_at.isoformat()}))
    monkeypatch.setattr(gateway, 'live', c.gateway.live)
    def fail(*_args, **_kwargs):
        raise RuntimeError('合成読取失敗')
    c.wiring.dispatcher._dispatcher._targets[Tool.ZOHO].get_record = fail
    assert gateway.dispatch('action', 'child', c.event.properties) == 'partial'
    assert c.saved == []
