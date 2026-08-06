from src.db_schema.product import PRODUCT_SCHEMA
from src.db_schema.base import PropertyType


def test_product_schema_notion_database_id_matches_real_data() -> None:
    assert PRODUCT_SCHEMA.notion_database_id == "3b4d8ea8-d4f3-80ed-a431-c2e4f5561fd6"


def test_product_schema_title_property_name_is_namae() -> None:
    assert PRODUCT_SCHEMA.title_property.name == "名前"
    assert PRODUCT_SCHEMA.title_property.property_type == PropertyType.TITLE
