from __future__ import annotations

import pytest

from src.db_schema.registry import get_schema
from src.sync_engine.webhook_handlers.zoho_field_transforms import (
    ZOHO_LABEL_FIELD_MAPPINGS,
)


def test_all_mapped_notion_properties_exist_in_schema() -> None:
    # shirokuma-secレビューWARN対応（2026-08-14、kintone_field_transforms.pyの同種テストと
    # 同じ理由）: ZOHO_LABEL_FIELD_MAPPINGSのNotionプロパティ名がタイポ等で実スキーマに存在
    # しない場合、Dispatcher側のKeyErrorガードでそのフィールドだけスキップされ気づきにくい。
    for db_key, field_mapping in ZOHO_LABEL_FIELD_MAPPINGS.items():
        schema = get_schema(db_key)
        for zoho_label, (notion_property, _transform) in field_mapping.items():
            schema.get_property(notion_property)


# --- action.取引先/【Notion】取引先マスター（取引先マスターリレーション解決、2026-08-25、Round2） ---


def test_action_client_master_fields_map_to_relation_property() -> None:
    raw_name_property, raw_name_transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["取引先"]
    hint_property, hint_transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["【Notion】取引先マスター"]

    assert raw_name_property == "👨‍👩‍👧‍👦 取引先マスター"
    assert hint_property == "👨‍👩‍👧‍👦 取引先マスター"
    # 両ラベルとも同じ解決関数を指す（field22の埋め込みヒント優先、モジュールdocstring参照）。
    assert raw_name_transform is hint_transform


def test_action_client_master_field_resolves_via_relation_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.sync_engine.webhook_handlers import zoho_field_transforms as module

    monkeypatch.setattr(
        module, "resolve_zoho_action_client_master_relation", lambda **kwargs: "notion-page-1"
    )
    _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["取引先"]

    with module.zoho_action_relation_context("77", {"field6": "テスト商事"}, None):
        assert transform("テスト商事") == "notion-page-1"


def test_action_client_master_field_returns_skip_field_when_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.sync_engine.webhook_handlers import zoho_field_transforms as module

    monkeypatch.setattr(module, "resolve_zoho_action_client_master_relation", lambda **kwargs: None)
    _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["取引先"]

    with module.zoho_action_relation_context("77", {"field6": "曖昧な会社名"}, None):
        assert transform("曖昧な会社名") is module.SKIP_FIELD


def test_action_client_master_field_returns_skip_field_without_context() -> None:
    """zoho_action_relation_context()を経由せずに呼ばれた場合（配線ミス等）も安全側に倒し、
    既存のリレーションを誤って上書きしないようSKIP_FIELDを返すこと。"""
    from src.sync_engine.webhook_handlers import zoho_field_transforms as module

    _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["取引先"]

    assert transform("テスト商事") is module.SKIP_FIELD


def test_action_client_master_field_passes_full_context_to_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.sync_engine.webhook_handlers import zoho_field_transforms as module

    calls: list[dict] = []
    monkeypatch.setattr(
        module,
        "resolve_zoho_action_client_master_relation",
        lambda **kwargs: calls.append(kwargs) or "notion-page-1",
    )
    _, transform = ZOHO_LABEL_FIELD_MAPPINGS["action"]["【Notion】取引先マスター"]
    sentinel_zoho_client = object()

    with module.zoho_action_relation_context(
        "action-record-77", {"field22": "hint", "field6": "テスト商事"}, sentinel_zoho_client
    ):
        transform("hint")

    assert calls == [
        {
            "record_id": "action-record-77",
            "changed_values": {"field22": "hint", "field6": "テスト商事"},
            "zoho_client": sentinel_zoho_client,
        }
    ]


def test_project_relation_fields_are_intentionally_excluded() -> None:
    # 案件(project)リレーションは今回もスコープ外のまま（モジュールdocstring・
    # _ACTION_ZOHO_LABEL_TO_NOTION_FIELD直前のコメント参照）。
    assert "案件名" not in ZOHO_LABEL_FIELD_MAPPINGS["action"]
    assert "案件" not in ZOHO_LABEL_FIELD_MAPPINGS["action"]
