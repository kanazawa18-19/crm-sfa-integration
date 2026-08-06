from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from scripts import setup_notion_databases
from scripts.setup_notion_databases import (
    TARGET_DB_KEYS,
    build_property_payload,
    build_update_properties_payload,
    print_dry_run_plan,
    update_target_databases,
)
from src.db_schema.base import (
    DatabaseSchema,
    PropertyDefinition,
    PropertyType,
    RequirementLevel,
    SyncScope,
)
from src.db_schema.client_master import CLIENT_MASTER_SCHEMA
from src.db_schema.contact import CONTACT_SCHEMA
from src.db_schema.product import PRODUCT_SCHEMA
from src.db_schema.registry import ALL_SCHEMAS


def test_build_property_payload_title() -> None:
    prop = PropertyDefinition(
        name="ID",
        property_type=PropertyType.TITLE,
        requirement=RequirementLevel.REQUIRED,
        sync_scope=SyncScope.ALL_TOOLS,
    )

    assert build_property_payload(prop) == {"title": {}}


def test_build_property_payload_select_includes_options() -> None:
    prop = PropertyDefinition(
        name="顧客種別",
        property_type=PropertyType.SELECT,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
        options=("A", "B"),
    )

    assert build_property_payload(prop) == {
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

    payload = build_property_payload(prop)

    assert payload == {"select": {"options": [{"name": "初回接触"}, {"name": "契約済"}]}}


def test_build_property_payload_relation_resolves_target_via_registry() -> None:
    prop = CONTACT_SCHEMA.get_property("取引先マスター")

    payload = build_property_payload(prop)

    assert payload == {
        "relation": {
            "database_id": CLIENT_MASTER_SCHEMA.notion_database_id,
            "single_property": {},
        }
    }


def test_build_property_payload_relation_uses_single_property_not_dual() -> None:
    """shirokuma-secレビューBLOCKER: dual_propertyだとNotion API側の自動処理で参照先DB
    （取引先マスター等の実データを保持する既存4DB）にも逆参照プロパティが自動生成されて
    しまう。single_propertyであれば連絡先DB側の片方向リレーションのみが作成され、
    既存4DBには一切変更が加わらないことを検証する。"""
    prop = CONTACT_SCHEMA.get_property("取引先マスター")

    payload = build_property_payload(prop)

    assert "single_property" in payload["relation"]
    assert "dual_property" not in payload["relation"]


def test_build_property_payload_relation_missing_notion_id_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prop = PropertyDefinition(
        name="取引先マスター",
        property_type=PropertyType.RELATION,
        requirement=RequirementLevel.REQUIRED,
        sync_scope=SyncScope.ALL_TOOLS,
        relation_target="client_master",
    )
    schema_without_id = DatabaseSchema(
        key="client_master",
        display_name="取引先マスター",
        id_prefix="CLI-",
        kintone_key="dummy",
        zoho_key="dummy",
        zoho_api_module="Accounts",
        spreadsheet_sheet_name="取引先マスター",
        properties=(
            PropertyDefinition(
                name="ID",
                property_type=PropertyType.TITLE,
                requirement=RequirementLevel.REQUIRED,
                sync_scope=SyncScope.ALL_TOOLS,
            ),
        ),
        notion_database_id=None,
    )
    monkeypatch.setattr(setup_notion_databases, "get_schema", lambda key: schema_without_id)

    with pytest.raises(RuntimeError):
        build_property_payload(prop)


def test_build_property_payload_currency_uses_yen_format() -> None:
    prop = PropertyDefinition(
        name="粗利",
        property_type=PropertyType.CURRENCY,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
    )

    assert build_property_payload(prop) == {"number": {"format": "yen"}}


def test_build_property_payload_number_uses_number_format() -> None:
    prop = PropertyDefinition(
        name="施設数",
        property_type=PropertyType.NUMBER,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
    )

    assert build_property_payload(prop) == {"number": {"format": "number"}}


def test_build_property_payload_text_maps_to_rich_text() -> None:
    prop = PropertyDefinition(
        name="住所",
        property_type=PropertyType.TEXT,
        requirement=RequirementLevel.OPTIONAL,
        sync_scope=SyncScope.ALL_TOOLS,
    )

    assert build_property_payload(prop) == {"rich_text": {}}


def test_build_property_payload_unsupported_type_raises_value_error() -> None:
    prop = PropertyDefinition(
        name="粗利ロールアップ",
        property_type=PropertyType.ROLLUP,
        requirement=RequirementLevel.AUTO,
        sync_scope=SyncScope.INTERNAL,
    )

    with pytest.raises(ValueError, match="未対応の型です"):
        build_property_payload(prop)


def test_build_update_properties_payload_excludes_title_for_contact() -> None:
    payload = build_update_properties_payload(CONTACT_SCHEMA)

    expected_names = {
        p.name for p in CONTACT_SCHEMA.properties if p.property_type != PropertyType.TITLE
    }
    assert "名前" not in payload["properties"]
    assert set(payload["properties"].keys()) == expected_names
    assert payload["properties"]["取引先マスター"] == {
        "relation": {
            "database_id": CLIENT_MASTER_SCHEMA.notion_database_id,
            "single_property": {},
        }
    }


def test_build_update_properties_payload_excludes_title_for_product() -> None:
    payload = build_update_properties_payload(PRODUCT_SCHEMA)

    expected_names = {
        p.name for p in PRODUCT_SCHEMA.properties if p.property_type != PropertyType.TITLE
    }
    assert "名前" not in payload["properties"]
    assert set(payload["properties"].keys()) == expected_names


def test_target_db_keys_limited_to_contact_and_product() -> None:
    assert set(TARGET_DB_KEYS) == {"contact", "product"}


def test_update_target_databases_only_patches_contact_and_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_patch = MagicMock(return_value=mock_response)
    monkeypatch.setattr("requests.patch", mock_patch)

    update_target_databases(api_key="secret-key")

    assert mock_patch.call_count == 2
    called_urls = {call.args[0] for call in mock_patch.call_args_list}
    assert called_urls == {
        f"https://api.notion.com/v1/databases/{CONTACT_SCHEMA.notion_database_id}",
        f"https://api.notion.com/v1/databases/{PRODUCT_SCHEMA.notion_database_id}",
    }
    # 既存4DB（実データを保持する稼働中DB）へのリクエストは一切発生しない。
    untouched_schemas = [s for s in ALL_SCHEMAS if s.key not in TARGET_DB_KEYS]
    untouched_urls = {
        f"https://api.notion.com/v1/databases/{s.notion_database_id}" for s in untouched_schemas
    }
    assert called_urls.isdisjoint(untouched_urls)


def test_update_target_databases_sends_relation_and_title_excluded_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_response = MagicMock()
    mock_response.raise_for_status.return_value = None
    mock_patch = MagicMock(return_value=mock_response)
    monkeypatch.setattr("requests.patch", mock_patch)

    update_target_databases(api_key="secret-key")

    payloads_by_url: dict[str, Any] = {
        call.args[0]: call.kwargs["json"] for call in mock_patch.call_args_list
    }
    contact_payload = payloads_by_url[
        f"https://api.notion.com/v1/databases/{CONTACT_SCHEMA.notion_database_id}"
    ]
    assert "名前" not in contact_payload["properties"]
    assert contact_payload["properties"]["取引先マスター"]["relation"]["database_id"] == (
        CLIENT_MASTER_SCHEMA.notion_database_id
    )


def test_print_dry_run_plan_distinguishes_existing_and_target_dbs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    print_dry_run_plan()
    output = capsys.readouterr().out

    assert "連絡先DB" in output
    assert "サービス・商品DB" in output
    assert "変更なし（既存DBのため）" in output
    for schema in ALL_SCHEMAS:
        if schema.key not in TARGET_DB_KEYS:
            assert f"[{schema.display_name}] (key={schema.key}) -> 変更なし（既存DBのため）" in output
