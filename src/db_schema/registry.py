"""Notion 6DB全体のレジストリ。scripts/ の自動作成スクリプトや同期エンジンから参照する。"""

from __future__ import annotations

from src.db_schema.action import ACTION_SCHEMA
from src.db_schema.base import DatabaseSchema
from src.db_schema.chain import CHAIN_SCHEMA
from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.db_schema.contact import CONTACT_SCHEMA
from src.db_schema.product import PRODUCT_SCHEMA
from src.db_schema.project import PROJECT_SCHEMA

# 02_DB構成一覧の並び順（①〜⑥）を踏襲する。
ALL_SCHEMAS: tuple[DatabaseSchema, ...] = (
    CLIENT_MASTER_SCHEMA,
    CHAIN_SCHEMA,
    CONTACT_SCHEMA,
    PROJECT_SCHEMA,
    PRODUCT_SCHEMA,
    ACTION_SCHEMA,
)

SCHEMAS_BY_KEY: dict[str, DatabaseSchema] = {schema.key: schema for schema in ALL_SCHEMAS}


def get_schema(key: str) -> DatabaseSchema:
    try:
        return SCHEMAS_BY_KEY[key]
    except KeyError as exc:
        raise KeyError(f"unknown db_schema key: {key!r}") from exc
