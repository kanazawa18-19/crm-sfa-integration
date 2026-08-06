from src.db_schema.contact import CONTACT_SCHEMA
from src.db_schema.base import PropertyType


def test_contact_schema_notion_database_id_matches_real_data() -> None:
    assert CONTACT_SCHEMA.notion_database_id == "3b4d8ea8-d4f3-808d-9853-d9cdd3de39ae"


def test_contact_schema_title_property_name_is_namae() -> None:
    assert CONTACT_SCHEMA.title_property.name == "名前"
    assert CONTACT_SCHEMA.title_property.property_type == PropertyType.TITLE
