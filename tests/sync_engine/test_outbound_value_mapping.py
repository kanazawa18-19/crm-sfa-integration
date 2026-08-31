"""Notion→Zoho の選択肢の読み替え（2026-08-31）。

対応表は**手で書かない**。Zohoの実際の選択肢一覧を、既にある「Zoho→Notion」の
取り込み変換に通し、その結果を反転して作る。取り込み側を直せば書き込み側も追随する。
"""

from __future__ import annotations

from src.sync_engine.outbound_value_mapping import (
    translate_choice_value,
    unmapped_notion_values,
    zoho_outbound_value_maps,
)


def test_billing_type_is_inverted_from_the_inbound_table() -> None:
    """課金形態の取り込みは辞書なので、そのまま反転できる。"""
    table = zoho_outbound_value_maps()["product"]["課金形態"]

    assert table["月額ストック"] == "ランニング"
    assert table["イニシャルスポット"] == "ショット"
    assert table["成果報酬"] == "成果報酬"


def test_stage_values_are_mostly_translatable() -> None:
    """営業ステータスは32値中30値が読み替えできる（実測、2026-08-31）。"""
    table = zoho_outbound_value_maps()["project"]["営業ステータス"]

    assert len(table) >= 30
    assert translate_choice_value("project", "営業ステータス", "失注") is not None


def test_values_without_a_counterpart_are_not_sent() -> None:
    """Zohoに無い選択肢は送らない。送っても弾かれるだけ。"""
    assert translate_choice_value("project", "営業ステータス", "アポ") is None
    assert translate_choice_value("project", "営業ステータス", "存在しない値") is None


def test_multi_select_is_all_or_nothing() -> None:
    """複数選択は1つでも読み替えられなければ送らない。

    読み替えられた分だけ送ると、**Notionでは付いている選択肢がZohoから消える**。
    """
    prefectures = zoho_outbound_value_maps()["client_master"].get("都道府県", {})
    known = next(iter(prefectures))

    assert translate_choice_value("client_master", "都道府県", [known]) == [
        prefectures[known]
    ]
    assert translate_choice_value("client_master", "都道府県", [known, "存在しない県"]) is None


def test_unknown_property_has_no_mapping() -> None:
    assert translate_choice_value("project", "存在しない項目", "x") is None


def test_unmapped_values_are_listed_for_review() -> None:
    """決まっていない値を一覧で出せること（実務で必要になった順に埋めるため）。"""
    missing = unmapped_notion_values()

    assert ("project", "営業ステータス") in missing
    assert "アポ" in missing[("project", "営業ステータス")]
