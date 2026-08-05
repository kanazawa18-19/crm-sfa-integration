from datetime import datetime, timezone

import pytest

from src.db_schema.base import Tool
from src.sync_engine.id_mapping import (
    ConflictError,
    DuplicateExternalIdError,
    IdMapping,
    SQLiteIdMappingStore,
)


@pytest.fixture
def store() -> SQLiteIdMappingStore:
    s = SQLiteIdMappingStore(":memory:")
    yield s
    s.close()


def test_get_returns_none_for_unknown_key(store: SQLiteIdMappingStore) -> None:
    assert store.get("CLI-999") is None


def test_upsert_then_get_roundtrip(store: SQLiteIdMappingStore) -> None:
    mapping = IdMapping(
        notion_key="CLI-001",
        db_key="client_master",
        kintone_id="1001",
        zoho_id="zoho-abc",
        spreadsheet_row=5,
        last_synced_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
    )

    store.upsert(mapping)
    result = store.get("CLI-001")

    assert result == mapping


def test_upsert_updates_existing_record(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="1001"))
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="9999"))

    result = store.get("CLI-001")

    assert result is not None
    assert result.kintone_id == "9999"


def test_delete_removes_record(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master"))

    store.delete("CLI-001")

    assert store.get("CLI-001") is None


def test_delete_nonexistent_key_is_noop(store: SQLiteIdMappingStore) -> None:
    store.delete("CLI-does-not-exist")


def test_find_by_external_id_kintone(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="1001"))

    result = store.find_by_external_id(Tool.KINTONE, "1001")

    assert result is not None
    assert result.notion_key == "CLI-001"


def test_find_by_external_id_zoho(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", zoho_id="zoho-abc"))

    result = store.find_by_external_id(Tool.ZOHO, "zoho-abc")

    assert result is not None
    assert result.notion_key == "CLI-001"


def test_find_by_external_id_spreadsheet_row(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", spreadsheet_row=42))

    result = store.find_by_external_id(Tool.SPREADSHEET, "42")

    assert result is not None
    assert result.notion_key == "CLI-001"


def test_find_by_external_id_returns_none_when_not_found(store: SQLiteIdMappingStore) -> None:
    assert store.find_by_external_id(Tool.KINTONE, "no-such-id") is None


def test_find_by_external_id_unsupported_tool_raises(store: SQLiteIdMappingStore) -> None:
    with pytest.raises(ValueError):
        store.find_by_external_id(Tool.NOTION, "CLI-001")


def test_update_last_synced_at(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master"))
    synced_at = datetime(2026, 8, 5, 9, 30, 0, tzinfo=timezone.utc)

    store.update_last_synced_at("CLI-001", synced_at)

    assert store.get("CLI-001").last_synced_at == synced_at


def test_update_last_synced_at_unknown_key_raises(store: SQLiteIdMappingStore) -> None:
    with pytest.raises(KeyError):
        store.update_last_synced_at("CLI-does-not-exist", datetime.now(timezone.utc))


def test_list_by_db(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master"))
    store.upsert(IdMapping(notion_key="CLI-002", db_key="client_master"))
    store.upsert(IdMapping(notion_key="MSA-PJ-001", db_key="project"))

    results = store.list_by_db("client_master")

    assert {r.notion_key for r in results} == {"CLI-001", "CLI-002"}


def test_list_by_db_returns_empty_for_unknown_db(store: SQLiteIdMappingStore) -> None:
    assert store.list_by_db("no-such-db") == []


def test_upsert_duplicate_kintone_id_raises(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="1001"))

    with pytest.raises(DuplicateExternalIdError):
        store.upsert(IdMapping(notion_key="CLI-002", db_key="client_master", kintone_id="1001"))

    # 迷子レコードが作られていないことを確認する。
    assert store.get("CLI-002") is None


def test_upsert_duplicate_zoho_id_raises(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", zoho_id="zoho-abc"))

    with pytest.raises(DuplicateExternalIdError):
        store.upsert(IdMapping(notion_key="CLI-002", db_key="client_master", zoho_id="zoho-abc"))


def test_upsert_duplicate_spreadsheet_row_raises(store: SQLiteIdMappingStore) -> None:
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", spreadsheet_row=42))

    with pytest.raises(DuplicateExternalIdError):
        store.upsert(IdMapping(notion_key="CLI-002", db_key="client_master", spreadsheet_row=42))


def test_upsert_same_notion_key_reusing_own_external_id_is_allowed(
    store: SQLiteIdMappingStore,
) -> None:
    """同一notion_keyに対する再upsert（自分自身の外部IDそのまま）は重複エラーにならない。"""
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="1001"))

    store.upsert(
        IdMapping(
            notion_key="CLI-001",
            db_key="client_master",
            kintone_id="1001",
            zoho_id="zoho-new",
        )
    )

    result = store.get("CLI-001")
    assert result is not None
    assert result.zoho_id == "zoho-new"


def test_upsert_null_external_ids_can_coexist(store: SQLiteIdMappingStore) -> None:
    """外部ID未連携（None）のレコードは複数あってもUNIQUE制約に抵触しない。"""
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master"))
    store.upsert(IdMapping(notion_key="CLI-002", db_key="client_master"))

    assert store.get("CLI-001") is not None
    assert store.get("CLI-002") is not None


def test_upsert_new_record_with_expected_none_succeeds(store: SQLiteIdMappingStore) -> None:
    store.upsert(
        IdMapping(notion_key="CLI-001", db_key="client_master"),
        expected_last_synced_at=None,
    )

    assert store.get("CLI-001") is not None


def test_upsert_with_matching_expected_last_synced_at_succeeds(
    store: SQLiteIdMappingStore,
) -> None:
    synced_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", last_synced_at=synced_at))

    store.upsert(
        IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="9999"),
        expected_last_synced_at=synced_at,
    )

    assert store.get("CLI-001").kintone_id == "9999"


def test_upsert_with_stale_expected_last_synced_at_raises_conflict(
    store: SQLiteIdMappingStore,
) -> None:
    synced_at = datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)
    store.upsert(IdMapping(notion_key="CLI-001", db_key="client_master", last_synced_at=synced_at))

    stale = datetime(2026, 7, 1, 0, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ConflictError):
        store.upsert(
            IdMapping(notion_key="CLI-001", db_key="client_master", kintone_id="9999"),
            expected_last_synced_at=stale,
        )

    # コンフリクト時はレコードが上書きされていないこと。
    assert store.get("CLI-001").kintone_id is None
