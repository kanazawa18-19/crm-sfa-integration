from src.db_schema.chain import CHAIN_SCHEMA
from src.db_schema.base import PropertyType


def test_chain_schema_notion_database_id_matches_real_data() -> None:
    assert CHAIN_SCHEMA.notion_database_id == "d6cb6fe0-5667-416c-a416-ac321d2ea52a"


def test_chain_schema_approach_status_is_status_type() -> None:
    prop = CHAIN_SCHEMA.get_property("アプローチ状況")
    assert prop.property_type == PropertyType.STATUS


def test_chain_schema_approach_status_options_match_real_data() -> None:
    prop = CHAIN_SCHEMA.get_property("アプローチ状況")
    assert set(prop.options) == {
        "未アプローチ",
        "連絡済み（アポNG）",
        "連絡済み（担当者未達）",
        "連絡済み（返信待ち）",
        "アポ調整中",
        "アポ確定済み",
        "提案済み",
        "トライアル",
        "【追加提案】別サービス",
        "【追加提案】横展開",
        "【一部】受注",
        "【全施設】受注",
        "失注",
    }
    assert len(prop.options) == 13
