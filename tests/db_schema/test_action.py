from src.db_schema.action import ACTION_SCHEMA
from src.db_schema.base import PropertyType


def test_action_schema_notion_database_id_matches_real_data() -> None:
    assert ACTION_SCHEMA.notion_database_id == "d1e4a612-560e-4eb9-8212-053f3901790a"


def test_action_schema_title_property_name_is_exact() -> None:
    assert ACTION_SCHEMA.title_property.name == "商談回数・電話回数・メール回数（何回目）"
    assert ACTION_SCHEMA.title_property.property_type == PropertyType.TITLE


def test_action_schema_project_relation_target_is_project() -> None:
    prop = ACTION_SCHEMA.get_property("案件名")
    assert prop.property_type == PropertyType.RELATION
    assert prop.relation_target == "project"


def test_action_schema_chain_relation_property_name_and_target() -> None:
    prop = ACTION_SCHEMA.get_property("👯‍♀️ チェーンリスト")
    assert prop.property_type == PropertyType.RELATION
    assert prop.relation_target == "chain"


def test_action_schema_client_master_relation_property_name_and_target() -> None:
    prop = ACTION_SCHEMA.get_property("👨‍👩‍👧‍👦 取引先マスター")
    assert prop.property_type == PropertyType.RELATION
    assert prop.relation_target == "client_master"
