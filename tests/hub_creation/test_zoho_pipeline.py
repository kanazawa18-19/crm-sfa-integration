"""既定設定の採用と、曖昧・未対応ステータスの作成前保留を確認する。"""
from types import SimpleNamespace
import pytest
from src.hub_creation.domain import CreationHeld
from src.hub_creation.zoho_pipeline import default_pipeline
from src.hub_creation.adapters import ZohoCreationAdapter


def pipeline():
    return {'id': '22', 'default': True, 'actual_value': '標準',
            'maps': [{'actual_value': '商談中'}]}


def test_explicit_default_and_its_layout_are_used():
    other = dict(pipeline(), id='33', default=False, actual_value='別部署')
    assert default_pipeline([('1', []), ('2', [other, pipeline()])], '商談中') == ('標準', '2')


@pytest.mark.parametrize('change', [
    {'default': False}, {'default': 'true'}, {'id': None}, {'actual_value': ''},
    {'maps': []}, {'maps': [{}]}, {'maps': [{'actual_value': '別の状態'}]},
])
def test_incomplete_or_nondefault_pipeline_is_held(change):
    with pytest.raises(CreationHeld):
        default_pipeline([('2', [dict(pipeline(), **change)])], '商談中')


def test_multiple_defaults_or_duplicate_ids_are_held():
    for extra in [pipeline(), dict(pipeline(), id='33')]:
        with pytest.raises(CreationHeld):
            default_pipeline([('2', [pipeline(), extra])], '商談中')


@pytest.mark.parametrize('metadata', [None, {}, 'invalid'])
def test_unknown_pipeline_list_is_held(metadata):
    with pytest.raises(CreationHeld):
        default_pipeline([('2', metadata)], '商談中')


def test_adapter_adds_verified_pipeline_and_layout_before_post(monkeypatch):
    fields = [{'api_name': key, 'data_type': 'text' if key == 'Deal_Name' else 'picklist', 'system_mandatory': True}
              for key in ['Deal_Name', 'Stage', 'Pipeline']]
    class Client:
        metadata = {'pipeline': [pipeline()]}
        calls = []
        def _request(self, method, path, **kwargs):
            self.calls.append((method, path))
            if 'settings/fields?' in path:
                assert kwargs == {'api_version': 'v8'}
                data = {'fields': fields}
            elif 'settings/layouts?' in path:
                assert kwargs == {'api_version': 'v8'}
                data = {'layouts': [
                    {'id': '1', 'sections': [{'fields': [{'api_name': 'OtherLayoutOnly', 'required': True}]}]},
                    {'id': '2', 'sections': [{'fields': []}]},
                ]}
            elif 'settings/pipeline?' in path:
                assert kwargs == {'api_version': 'v8'}
                data = self.metadata if 'layout_id=2' in path else {'pipeline': []}
            else:
                data = {'data': [], 'info': {'more_records': False}}
            return SimpleNamespace(ok=True, status_code=200, json=lambda: data)
    client = Client()
    adapter = ZohoCreationAdapter(client)
    payload = adapter.plan('project', {'案件名': '専用試験', '営業ステータス': '商談中'})
    assert payload == {'Deal_Name': '専用試験', 'Stage': '商談中', 'Pipeline': '標準', 'Layout': {'id': '2'}}
    assert all(method == 'GET' for method, _ in client.calls)
    client.metadata = {}
    with pytest.raises(CreationHeld):
        adapter.plan('project', {'案件名': '専用試験', '営業ステータス': '商談中'})


def test_pipeline_creation_uses_v8_only_for_deals():
    calls = []
    client = SimpleNamespace(insert_record=lambda module, payload, **kw: calls.append((module, kw)) or '123')
    adapter = ZohoCreationAdapter(client)
    assert adapter.create('project', {'Pipeline': '標準'}) == '123'
    adapter.create('chain', {'Name': '専用試験'})
    assert calls == [('Deals', {'api_version': 'v8'}), ('CustomModule3', {})]


@pytest.mark.parametrize('label,unused,held', [('テレアポ', False, False), ('変更済み', False, True), ('テレアポ', True, True)])
def test_changed_lead_source_metadata_stops_creation(label, unused, held):
    from src.hub_creation.creation_payload import validate_lead_source_metadata
    fields = [{'api_name': 'field65', 'pick_list_values': [
        {'actual_value': '選択肢1', 'display_value': label, 'type': 'unused' if unused else 'used'}]}]
    if held:
        with pytest.raises(CreationHeld, match='内部値'):
            validate_lead_source_metadata({'field65': ['選択肢1']}, fields)
    else:
        validate_lead_source_metadata({'field65': ['選択肢1']}, fields)
