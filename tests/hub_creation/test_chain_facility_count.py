"""シートが施設数を数値で返す場合だけ、安全に文字列へ合わせる。"""
import pytest
from src.hub_creation.domain import sheet_properties, CreationHeld


@pytest.mark.parametrize(('value', 'expected'), [(0, '0'), (12, '12'), (12.0, '12'), ('約12', '約12')])
def test_chain_numeric_facility_count(value, expected):
    result = sheet_properties('chain', {'グループ名': '検証チェーン', '施設数': value})
    assert result['施設数'] == expected


@pytest.mark.parametrize('value', [True, False, -1, -0.5, 1.2, float('inf'), float('-inf'), float('nan'), [], {}])
def test_invalid_count_is_held(value):
    with pytest.raises(CreationHeld):
        sheet_properties('chain', {'グループ名': '検証チェーン', '施設数': value})


def test_other_text_fields_are_not_coerced():
    with pytest.raises(CreationHeld):
        sheet_properties('chain', {'グループ名': 123, '施設数': 0})
