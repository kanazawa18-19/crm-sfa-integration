import pytest

from scripts.setup_notion_databases import (
    build_create_database_payload,
    build_property_payload,
    build_relation_patch_payload,
)
from src.db_schema.base import (
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
)
from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.db_schema.product import PRODUCT_SCHEMA


def test_build_property_payload_title() -> None:
    prop = PropertyDefinition(
        name="ID",
        property_type=PropertyType.TITLE,
        requirement=RequirementLevel.REQUIRED,
        sync_scope=SyncScope.ALL_TOOLS,
    )

    assert build_property_payload(prop, created_db_ids={}) == {"title": {}}


def test_build_property_payload_select_includes_options() -> None:
    prop = PropertyDefinition(
        name="顧客種別",
        property_type=PropertyType.SELECT,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
        options=("A", "B"),
    )

    assert build_property_payload(prop, created_db_ids={}) == {
        "select": {"options": [{"name": "A"}, {"name": "B"}]}
    }


def test_build_property_payload_status_falls_back_to_select() -> None:
    prop = PropertyDefinition(
        name="営業ステータス",
        property_type=PropertyType.STATUS,
        requirement=RequirementLevel.REQUIRED,
        sync_scope=SyncScope.ALL_TOOLS,
        options=("初回接触", "契約済"),
    )

    payload = build_property_payload(prop, created_db_ids={})

    assert payload == {"select": {"options": [{"name": "初回接触"}, {"name": "契約済"}]}}


def test_build_property_payload_relation_uses_dual_property() -> None:
    prop = PropertyDefinition(
        name="取引先マスター",
        property_type=PropertyType.RELATION,
        requirement=RequirementLevel.REQUIRED,
        sync_scope=SyncScope.ALL_TOOLS,
        relation_target="client_master",
    )

    payload = build_property_payload(prop, created_db_ids={"client_master": "db-abc"})

    assert payload == {"relation": {"database_id": "db-abc", "dual_property": {}}}


def test_build_property_payload_relation_missing_target_raises() -> None:
    prop = PropertyDefinition(
        name="取引先マスター",
        property_type=PropertyType.RELATION,
        requirement=RequirementLevel.REQUIRED,
        sync_scope=SyncScope.ALL_TOOLS,
        relation_target="client_master",
    )

    with pytest.raises(RuntimeError):
        build_property_payload(prop, created_db_ids={})


def test_build_property_payload_currency_uses_yen_format() -> None:
    prop = PropertyDefinition(
        name="粗利",
        property_type=PropertyType.CURRENCY,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
    )

    assert build_property_payload(prop, created_db_ids={}) == {"number": {"format": "yen"}}


def test_build_property_payload_number_uses_number_format() -> None:
    prop = PropertyDefinition(
        name="施設数",
        property_type=PropertyType.NUMBER,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
    )

    assert build_property_payload(prop, created_db_ids={}) == {"number": {"format": "number"}}


def test_build_property_payload_text_maps_to_rich_text() -> None:
    prop = PropertyDefinition(
        name="住所",
        property_type=PropertyType.TEXT,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
    )

    assert build_property_payload(prop, created_db_ids={}) == {"rich_text": {}}


def test_build_create_database_payload_excludes_relation_properties() -> None:
    payload = build_create_database_payload(CLIENT_MASTER_SCHEMA, parent_page_id="page-123")

    assert payload["parent"] == {"type": "page_id", "page_id": "page-123"}
    assert payload["title"] == [
        {"type": "text", "text": {"content": CLIENT_MASTER_SCHEMA.display_name}}
    ]
    property_names = set(payload["properties"].keys())
    relation_names = {
        p.name for p in CLIENT_MASTER_SCHEMA.properties if p.property_type == PropertyType.RELATION
    }
    assert relation_names & property_names == set()
    assert "取引先ID" in property_names


def test_build_create_database_payload_without_relations_includes_all_properties() -> None:
    payload = build_create_database_payload(PRODUCT_SCHEMA, parent_page_id="page-123")

    expected_names = {p.name for p in PRODUCT_SCHEMA.properties}
    assert set(payload["properties"].keys()) == expected_names


def test_build_relation_patch_payload_only_includes_relation_properties() -> None:
    created_db_ids = {"client_master": "db-cli", "chain": "db-chain"}

    payload = build_relation_patch_payload(CLIENT_MASTER_SCHEMA, created_db_ids)

    relation_names = {
        p.name for p in CLIENT_MASTER_SCHEMA.properties if p.property_type == PropertyType.RELATION
    }
    assert set(payload["properties"].keys()) == relation_names
    for prop_payload in payload["properties"].values():
        assert "dual_property" in prop_payload["relation"]


def test_build_relation_patch_payload_no_relations_returns_empty_properties() -> None:
    payload = build_relation_patch_payload(PRODUCT_SCHEMA, created_db_ids={})

    assert payload == {"properties": {}}
