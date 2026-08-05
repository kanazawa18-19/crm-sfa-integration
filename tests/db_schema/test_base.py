import pytest

from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
)


def _title_property(name: str = "ID") -> PropertyDefinition:
    return PropertyDefinition(
        name=name,
        property_type=PropertyType.TITLE,
        requirement=RequirementLevel.REQUIRED,
        sync_scope=SyncScope.ALL_TOOLS,
    )


def test_property_definition_relation_without_target_raises() -> None:
    with pytest.raises(ValueError):
        PropertyDefinition(
            name="取引先マスター",
            property_type=PropertyType.RELATION,
            requirement=RequirementLevel.REQUIRED,
            sync_scope=SyncScope.ALL_TOOLS,
        )


def test_property_definition_relation_with_target_ok() -> None:
    prop = PropertyDefinition(
        name="取引先マスター",
        property_type=PropertyType.RELATION,
        requirement=RequirementLevel.REQUIRED,
        sync_scope=SyncScope.ALL_TOOLS,
        relation_target="client_master",
    )
    assert prop.relation_target == "client_master"


def test_database_schema_requires_exactly_one_title_property() -> None:
    with pytest.raises(ValueError):
        DatabaseSchema(
            key="dummy",
            display_name="ダミーDB",
            id_prefix="DMY-",
            kintone_key="dummy",
            zoho_key="dummy",
            properties=(),
        )


def test_database_schema_rejects_multiple_title_properties() -> None:
    with pytest.raises(ValueError):
        DatabaseSchema(
            key="dummy",
            display_name="ダミーDB",
            id_prefix="DMY-",
            kintone_key="dummy",
            zoho_key="dummy",
            properties=(_title_property("ID1"), _title_property("ID2")),
        )


def test_database_schema_rejects_duplicate_property_names() -> None:
    with pytest.raises(ValueError):
        DatabaseSchema(
            key="dummy",
            display_name="ダミーDB",
            id_prefix="DMY-",
            kintone_key="dummy",
            zoho_key="dummy",
            properties=(
                _title_property("ID"),
                PropertyDefinition(
                    name="同名",
                    property_type=PropertyType.TEXT,
                    requirement=RequirementLevel.OPTIONAL,
                    sync_scope=SyncScope.ALL_TOOLS,
                ),
                PropertyDefinition(
                    name="同名",
                    property_type=PropertyType.TEXT,
                    requirement=RequirementLevel.OPTIONAL,
                    sync_scope=SyncScope.ALL_TOOLS,
                ),
            ),
        )


def test_database_schema_valid_definition_succeeds() -> None:
    schema = DatabaseSchema(
        key="dummy",
        display_name="ダミーDB",
        id_prefix="DMY-",
        kintone_key="dummy",
        zoho_key="dummy",
        properties=(_title_property("ID"),),
    )
    assert schema.title_property.name == "ID"
    assert schema.get_property("ID").name == "ID"


def test_database_schema_get_property_unknown_raises_keyerror() -> None:
    schema = DatabaseSchema(
        key="dummy",
        display_name="ダミーDB",
        id_prefix="DMY-",
        kintone_key="dummy",
        zoho_key="dummy",
        properties=(_title_property("ID"),),
    )
    with pytest.raises(KeyError):
        schema.get_property("存在しない")
