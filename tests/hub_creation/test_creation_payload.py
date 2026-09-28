from types import SimpleNamespace
import pytest
from src.hub_creation.creation_payload import complete_zoho_payload
from src.hub_creation.domain import CreationHeld
from src.hub_creation.adapters import normalize_creation_payload


def test_contact_uses_separate_name_inputs_without_splitting_title():
    result = complete_zoho_payload('contact', {'名前': 'フルネーム', '姓': '姓入力', '名': '名入力', '取引先マスター': ['page']},
        {'Full_Name': 'フルネーム'}, get_mapping=lambda _: SimpleNamespace(db_key='client_master', zoho_id='123'))
    assert result == {'Last_Name': '姓入力', 'First_Name': '名入力', 'field25': {'id': '123'}}


@pytest.mark.parametrize('ids', [['a','b'], 'a'])
def test_relation_cannot_guess_single_target(ids):
    with pytest.raises(CreationHeld):
        complete_zoho_payload('contact', {'取引先マスター': ids}, {}, get_mapping=lambda _: None)


def test_related_target_must_be_mapped_to_the_correct_module():
    with pytest.raises(CreationHeld):
        complete_zoho_payload('contact', {'取引先マスター': ['page']}, {},
            get_mapping=lambda _: SimpleNamespace(db_key='product', zoho_id='123'))


def test_verified_lookup_ids_only_are_accepted():
    fields = {'field25': {'data_type':'lookup'}}
    assert normalize_creation_payload({'field25': {'id':'123'}}, fields, lookup_fields={'field25'}) == {'field25': {'id':'123'}}
    for value in ({'name':'同名'}, {'id':'123','name':'同名'}, '123'):
        with pytest.raises(CreationHeld):
            normalize_creation_payload({'field25':value}, fields, lookup_fields={'field25'})


def test_kintone_choice_reverse_requires_unique_correspondence():
    from src.hub_creation.creation_payload import kintone_choice_payload
    result = kintone_choice_payload('action', {'アクション種別': 'オンライン商談'},
        {'actionContent': {'type':'DROP_DOWN','options':{'WEB商談':{},'その他':{}}}})
    assert result == {'actionContent':'WEB商談'}


def test_creation_rollup_uses_actual_users_and_services_only():
    from src.hub_creation.creation_payload import creation_rollup
    assert creation_rollup({'type':'rollup','rollup':{'type':'incomplete'}}) is None
    assert creation_rollup({'type':'rollup','rollup':{'type':'array','array':[{'type':'people','people':[{'id':'u'}]}]}}) == ['u']


def test_blank_choice_does_not_select_matching_external_default(monkeypatch):
    from src.hub_creation.creation_payload import kintone_choice_payload
    from src.sync_engine.webhook_handlers.kintone_field_transforms import KINTONE_FIELD_TRANSFORMS
    monkeypatch.setitem(KINTONE_FIELD_TRANSFORMS, 'project', {'field': ('項目', lambda option: None)})
    assert kintone_choice_payload('project', {'項目': None}, {'field': {
        'type': 'DROP_DOWN', 'options': {'初期値': {}}}}) == {}
