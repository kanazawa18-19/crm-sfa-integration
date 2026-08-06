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
            zoho_api_module="Dummy",
            spreadsheet_sheet_name="ダミー",
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
            zoho_api_module="Dummy",
            spreadsheet_sheet_name="ダミー",
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
            zoho_api_module="Dummy",
            spreadsheet_sheet_name="ダミー",
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
        zoho_api_module="Dummy",
        spreadsheet_sheet_name="ダミー",
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
        zoho_api_module="Dummy",
        spreadsheet_sheet_name="ダミー",
        properties=(_title_property("ID"),),
    )
    with pytest.raises(KeyError):
        schema.get_property("存在しない")


def test_database_schema_notion_database_id_defaults_to_none() -> None:
    schema = DatabaseSchema(
        key="dummy",
        display_name="ダミーDB",
        id_prefix="DMY-",
        kintone_key="dummy",
        zoho_key="dummy",
        zoho_api_module="Dummy",
        spreadsheet_sheet_name="ダミー",
        properties=(_title_property("ID"),),
    )
    assert schema.notion_database_id is None


def test_database_schema_notion_database_id_can_be_set() -> None:
    schema = DatabaseSchema(
        key="dummy",
        display_name="ダミーDB",
        id_prefix="DMY-",
        kintone_key="dummy",
        zoho_key="dummy",
        zoho_api_module="Dummy",
        spreadsheet_sheet_name="ダミー",
        properties=(_title_property("ID"),),
        notion_database_id="11111111-2222-3333-4444-555555555555",
    )
    assert schema.notion_database_id == "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize(
    "property_type",
    [
        PropertyType.ROLLUP,
        PropertyType.FORMULA,
        PropertyType.BUTTON,
        PropertyType.UNIQUE_ID,
        PropertyType.CREATED_TIME,
        PropertyType.LAST_EDITED_TIME,
        PropertyType.CREATED_BY,
    ],
)
def test_read_only_property_types_are_not_writable(property_type: PropertyType) -> None:
    prop = PropertyDefinition(
        name="読取専用",
        property_type=property_type,
        requirement=RequirementLevel.AUTO,
        sync_scope=SyncScope.INTERNAL,
    )
    assert prop.is_writable is False


@pytest.mark.parametrize(
    "property_type",
    [
        PropertyType.TITLE,
        PropertyType.TEXT,
        PropertyType.SELECT,
        PropertyType.STATUS,
        PropertyType.MULTI_SELECT,
        PropertyType.NUMBER,
        PropertyType.DATE,
        PropertyType.EMAIL,
        PropertyType.PHONE,
        PropertyType.URL,
        PropertyType.CHECKBOX,
        PropertyType.USER,
        PropertyType.FILES,
    ],
)
def test_writable_property_types_are_writable(property_type: PropertyType) -> None:
    prop = PropertyDefinition(
        name="書き込み可能",
        property_type=property_type,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
    )
    assert prop.is_writable is True


@pytest.mark.parametrize(
    "property_type",
    [
        PropertyType.ROLLUP,
        PropertyType.FORMULA,
        PropertyType.BUTTON,
        PropertyType.UNIQUE_ID,
        PropertyType.CREATED_TIME,
        PropertyType.LAST_EDITED_TIME,
        PropertyType.CREATED_BY,
    ],
)
@pytest.mark.parametrize(
    "sync_scope",
    [SyncScope.ALL_TOOLS, SyncScope.NOTION_ONLY, SyncScope.SPREADSHEET_ONLY],
)
def test_read_only_property_type_requires_internal_sync_scope(
    property_type: PropertyType, sync_scope: SyncScope
) -> None:
    with pytest.raises(ValueError):
        PropertyDefinition(
            name="読取専用",
            property_type=property_type,
            requirement=RequirementLevel.AUTO,
            sync_scope=sync_scope,
        )


def test_read_only_property_type_with_internal_sync_scope_succeeds() -> None:
    prop = PropertyDefinition(
        name="読取専用",
        property_type=PropertyType.ROLLUP,
        requirement=RequirementLevel.AUTO,
        sync_scope=SyncScope.INTERNAL,
    )
    assert prop.sync_scope == SyncScope.INTERNAL
