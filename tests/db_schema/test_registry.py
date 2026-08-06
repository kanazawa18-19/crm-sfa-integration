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


def test_existing_four_dbs_have_notion_database_id_set() -> None:
    """既存の稼働中Notionワークスペースに実在する4DBは固定のdatabase_idを持つ。"""
    for key in ("client_master", "chain", "project", "action"):
        schema = SCHEMAS_BY_KEY[key]
        assert schema.notion_database_id, f"{key}: notion_database_id must be set"


def test_new_two_dbs_have_notion_database_id_set() -> None:
    """新規作成した2DB（連絡先／サービス・商品）も接続済みのdatabase_idを持つ。"""
    for key in ("contact", "product"):
        schema = SCHEMAS_BY_KEY[key]
        assert schema.notion_database_id, f"{key}: notion_database_id must be set"


def test_read_only_properties_are_internal_scope_across_all_schemas() -> None:
    """is_writable=Falseなプロパティは、DatabaseSchemaの__post_init__バリデーションにより
    常にsync_scope=INTERNALであることが保証される（回帰チェック）。"""
    for schema in ALL_SCHEMAS:
        for prop in schema.properties:
            if not prop.is_writable:
                assert prop.sync_scope.value == "internal", (
                    f"{schema.key}.{prop.name}: read-only property must be INTERNAL scope"
                )
