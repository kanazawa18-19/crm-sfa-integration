import pytest
from scripts.prepare_creation_fields import desired_properties, additive_plan, FIELDS


def test_schema_additions_are_scoped_and_never_retype_existing_fields():
    assert len(FIELDS['project']) == 5
    desired = desired_properties('contact')
    assert set(desired) == {'姓', '名'}
    assert set(additive_plan('contact', {'姓': {'type':'rich_text'}, '既存項目': {'type':'title'}})) == {'名'}
    with pytest.raises(ValueError):
        additive_plan('contact', {'姓': {'type':'title'}})


def test_existing_relation_to_other_database_is_not_accepted():
    with pytest.raises(ValueError, match='関連先DB'):
        additive_plan('product', {'先方担当者': {'type':'relation','relation':{'database_id':'another-db'}}})
