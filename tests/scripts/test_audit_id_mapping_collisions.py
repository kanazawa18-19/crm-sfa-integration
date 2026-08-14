from __future__ import annotations

from src.db_schema.base import Tool
from src.sync_engine.id_mapping import IdMapping, SQLiteIdMappingStore
from scripts.audit_id_mapping_collisions import find_cross_db_key_collisions


def test_find_cross_db_key_collisions_detects_kintone_id_reused_across_db_keys() -> None:
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="PJ-001", db_key="project", kintone_id="45"))
    store.upsert(IdMapping(notion_key="ACT-001", db_key="action", kintone_id="45"))

    collisions = find_cross_db_key_collisions(store, ("project", "action"))

    assert len(collisions) == 1
    assert collisions[0].tool is Tool.KINTONE
    assert collisions[0].external_id == "45"
    assert collisions[0].notion_keys_by_db_key == {"project": "PJ-001", "action": "ACT-001"}


def test_find_cross_db_key_collisions_returns_empty_when_no_overlap() -> None:
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="PJ-001", db_key="project", kintone_id="45"))
    store.upsert(IdMapping(notion_key="ACT-001", db_key="action", kintone_id="99"))

    collisions = find_cross_db_key_collisions(store, ("project", "action"))

    assert collisions == []


def test_find_cross_db_key_collisions_ignores_none_values() -> None:
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(IdMapping(notion_key="PJ-001", db_key="project"))
    store.upsert(IdMapping(notion_key="ACT-001", db_key="action"))

    collisions = find_cross_db_key_collisions(store, ("project", "action"))

    assert collisions == []


def test_find_cross_db_key_collisions_checks_all_three_tools_independently() -> None:
    store = SQLiteIdMappingStore(":memory:")
    store.upsert(
        IdMapping(notion_key="PJ-001", db_key="project", kintone_id="1", zoho_id="z1", spreadsheet_row=1)
    )
    store.upsert(
        IdMapping(notion_key="ACT-001", db_key="action", kintone_id="1", zoho_id="z2", spreadsheet_row=1)
    )

    collisions = find_cross_db_key_collisions(store, ("project", "action"))

    tools_with_collisions = {c.tool for c in collisions}
    assert tools_with_collisions == {Tool.KINTONE, Tool.SPREADSHEET}
