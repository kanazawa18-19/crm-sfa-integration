import pytest

from src.db_schema.base import PropertyType
from src.db_schema.registry import ALL_SCHEMAS, SCHEMAS_BY_KEY, get_schema


def test_all_relation_targets_point_to_existing_schema_keys() -> None:
    """RELATION型プロパティの relation_target が実在するDBキー（自DB参照含む）を
    指していることを全DB分検証する整合性チェック。"""
    for schema in ALL_SCHEMAS:
        for prop in schema.properties:
            if prop.property_type != PropertyType.RELATION:
                continue
            assert prop.relation_target in SCHEMAS_BY_KEY, (
                f"{schema.key}.{prop.name}: unknown relation_target "
                f"{prop.relation_target!r}"
            )


def test_get_schema_returns_known_schema() -> None:
    schema = get_schema("client_master")
    assert schema.key == "client_master"


def test_get_schema_unknown_key_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        get_schema("no-such-schema")
