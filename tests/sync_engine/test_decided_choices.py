"""確定回答の変換と、備考本文を保持する境界。"""
from unittest.mock import Mock
import pytest
from src.sync_engine.decided_choices import first_touch, primary_controller, merge_choice_memo
from src.sync_engine.sync_targets.kintone_sync import KintoneSyncTarget
from src.sync_engine.sync_targets.zoho_sync import ZohoSyncTarget
from src.sync_engine.clients._http import ApiError, ConcurrentModificationError


def test_first_touch_keeps_original_order():
    assert first_touch(["パートナー", "テレアポ"]) == "代理店"
    assert first_touch(["テレアポ", "パートナー"]) == "営業"
    assert first_touch(["テレアポ", "未定義"]) is None
    assert first_touch([]) is None


def test_controller_priority_is_independent_of_input_order():
    assert primary_controller(["ねっぱん", "手間いらず", "リンカーン"]) == "リンカーン"
    assert primary_controller(["エアホスト", "Beds24"]) == primary_controller(["Beds24", "エアホスト"])


def test_memo_preserves_body_and_other_blocks():
    base = "手書き本文\n改行"
    memo = merge_choice_memo(base, "ファーストタッチ", ["テレアポ"], limit=2000)
    memo = merge_choice_memo(memo, "サイトコントローラー", ["ねっぱん"], limit=2000)
    new = merge_choice_memo(memo, "ファーストタッチ", ["パートナー"], limit=2000)
    assert new.startswith(base)
    assert "ねっぱん" in new and "テレアポ" not in new
    assert new.count("【CRM同期:ファーストタッチ】") == 1
    assert merge_choice_memo(new, "ファーストタッチ", ["パートナー"], limit=2000) == new
    with pytest.raises(ValueError):
        merge_choice_memo(base, "x", ["長い"], limit=1)


def test_kintone_writes_first_choice_and_all_values_with_version():
    c = Mock()
    c.get_record.return_value = {"$revision": "12", "文字列__複数行_": "既存本文"}
    t = KintoneSyncTarget(c, "test")
    props = {"ファーストタッチ": ["テレアポ", "パートナー"], "サイトコントローラー": ["ねっぱん"]}
    assert t.unsupported_properties(props, db_key="project") == frozenset()
    t.upsert_record("123", props, db_key="project", expected_version="12")
    payload = c.update_record.call_args.args[2]
    assert payload["ドロップダウン_4"] == "営業"
    assert payload["文字列__複数行_"].startswith("既存本文")
    assert "パートナー" in payload["文字列__複数行_"]
    assert c.update_record.call_args.kwargs["expected_version"] == "12"


def test_zoho_writes_scalar_and_preserves_memo():
    c = Mock()
    c.get_record.return_value = {"Modified_Time": "2026-09-28T00:00:00Z", "field70": "既存本文"}
    t = ZohoSyncTarget(c, "Deals", enabled=True)
    t.upsert_record("123", {"サイトコントローラー": ["ねっぱん", "リンカーン"]}, db_key="project")
    payload = c.update_record.call_args.args[2]
    assert payload["field20"] == "リンカーン"
    assert payload["field70"].startswith("既存本文")
    assert "ねっぱん、リンカーン" in payload["field70"]


@pytest.mark.parametrize("current", [None, {"$revision": "13"}, {"$revision": "12", "文字列__複数行_": "【CRM同期:ファーストタッチ】壊れた枠"}])
def test_kintone_does_not_write_on_missing_changed_or_invalid_memo(current):
    c = Mock()
    c.get_record.return_value = current
    with pytest.raises((ApiError, ConcurrentModificationError)):
        KintoneSyncTarget(c, "test").upsert_record("123", {"ファーストタッチ": ["テレアポ"]}, db_key="project", expected_version="12")
    c.update_record.assert_not_called()


def test_simultaneous_memo_edit_is_not_reverted():
    c = Mock()
    c.get_record.return_value = {"Modified_Time": "v1", "field70": "旧本文"}
    ZohoSyncTarget(c, "Deals", enabled=True).upsert_record(
        "123", {"メモ": "新本文", "サイトコントローラー": ["ねっぱん"]}, db_key="project")
    payload = c.update_record.call_args.args[2]
    assert payload["field70"].startswith("新本文")
    assert "旧本文" not in payload["field70"]


def test_roundtrip_restores_all_choices_and_preserves_external_addition():
    from src.sync_engine.decided_choices import controller_from_external
    memo = merge_choice_memo("本文", "サイトコントローラー", ["リンカーン", "ねっぱん"], limit=2000)
    assert controller_from_external("リンカーン", memo) == ["リンカーン", "ねっぱん"]
    assert controller_from_external("TEMAIRAZU", memo) == ["リンカーン", "ねっぱん", "手間いらず"]


def test_inbound_does_not_reduce_choices_without_saved_field():
    from src.sync_engine.webhook_handlers.zoho_field_transforms import ZOHO_LABEL_FIELD_MAPPINGS, SKIP_FIELD, zoho_action_relation_context
    transform = ZOHO_LABEL_FIELD_MAPPINGS["project"]["サイトコントローラー"][1]
    assert transform("リンカーン") is SKIP_FIELD
    memo = merge_choice_memo("本文", "サイトコントローラー", ["リンカーン", "ねっぱん"], limit=2000)
    with zoho_action_relation_context("123", {"field70": memo}, None):
        assert transform("リンカーン") == ["リンカーン", "ねっぱん"]


def test_memo_only_edit_keeps_choice_storage():
    from src.sync_engine.decided_choices import replace_memo_body, split_controller_memo
    old = merge_choice_memo("旧本文", "サイトコントローラー", ["リンカーン", "ねっぱん"], limit=2000)
    new = replace_memo_body(old, "新本文")
    assert split_controller_memo(new)[0] == "新本文"
    assert "ねっぱん" in new
    c = Mock()
    c.get_record.return_value = {"Modified_Time": "v1", "field70": old}
    ZohoSyncTarget(c, "Deals", enabled=True).upsert_record("123", {"メモ": "新本文"}, db_key="project")
    assert c.update_record.call_args.args[2]["field70"] == new


@pytest.mark.parametrize("memo", [None, "", "手書きのメモ"])
def test_inbound_without_saved_choices_is_held(memo):
    from src.sync_engine.decided_choices import controller_from_external
    from src.sync_engine.webhook_handlers.zoho_field_transforms import (
        ZOHO_LABEL_FIELD_MAPPINGS, SKIP_FIELD, zoho_action_relation_context,
    )
    assert controller_from_external("リンカーン", memo) is None
    transform = ZOHO_LABEL_FIELD_MAPPINGS["project"]["サイトコントローラー"][1]
    with zoho_action_relation_context("123", {"field70": memo}, None):
        assert transform("リンカーン") is SKIP_FIELD


@pytest.mark.parametrize("value", [None, "", [], [""], ["未知"], ["リンカーン", "未知"], [None], {}, 1])
@pytest.mark.parametrize("name", ["ファーストタッチ", "サイトコントローラー"])
@pytest.mark.parametrize("tool", ["zoho", "kintone"])
def test_invalid_choices_do_not_read_or_write(value, name, tool):
    client = Mock()
    target = ZohoSyncTarget(client, "Deals", enabled=True) if tool == "zoho" else KintoneSyncTarget(client, "test")
    assert target.upsert_record("123", {name: value}, db_key="project") is None
    client.get_record.assert_not_called()
    client.update_record.assert_not_called()


@pytest.mark.parametrize("current", [None, {}, {"Modified_Time": "v2"}, {
    "Modified_Time": "v1", "field70": "【CRM同期:サイトコントローラー】壊れた枠",
}])
def test_zoho_memo_validation_prevents_write(current):
    client = Mock()
    client.get_record.return_value = current
    with pytest.raises((ApiError, ConcurrentModificationError)):
        ZohoSyncTarget(client, "Deals", enabled=True).upsert_record(
            "123", {"サイトコントローラー": ["リンカーン"]},
            db_key="project", expected_version="v1",
        )
    client.update_record.assert_not_called()
