"""Notion→外部の項目名逆変換（`src/sync_engine/outbound_field_mapping.py`）。"""

from __future__ import annotations

from src.db_schema.base import PropertyType
from src.db_schema.registry import ALL_SCHEMAS
from src.sync_engine.outbound_field_mapping import (
    kintone_outbound_field_names,
    translate_properties,
    zoho_outbound_field_names,
)


def test_zoho_property_names_are_replaced_with_api_names() -> None:
    """Zohoのapi_nameは`field7`のような自動採番で、Notionのプロパティ名とは一致しない。"""
    table = zoho_outbound_field_names()

    assert table["project"]["案件名"] == "Deal_Name"
    assert table["client_master"]["TEL"] == "Phone"


def test_kintone_property_names_are_replaced_with_field_codes() -> None:
    """kintoneのフィールドコードは画面上のラベルと別物。"""
    table = kintone_outbound_field_names()

    assert table["client_master"]["取引先名"] == "顧客名"
    # 「月額費用」の実体は`初期費用_0`（提案料金（ランニング））。ラベルからは推測できない。
    assert table["project"]["月額費用"] == "初期費用_0"


def test_relation_properties_are_never_sent_outbound() -> None:
    """リレーションの値はNotionのページIDで、外部ツールには存在しない識別子。"""
    relations = {
        (schema.key, prop.name)
        for schema in ALL_SCHEMAS
        for prop in schema.properties
        if prop.property_type is PropertyType.RELATION
    }
    for table in (zoho_outbound_field_names(), kintone_outbound_field_names()):
        for db_key, mapping in table.items():
            for property_name in mapping:
                assert (db_key, property_name) not in relations


def test_choice_properties_are_excluded_until_value_conversion_exists() -> None:
    """選択肢の値は多対一で取り込んでいるため、機械的には逆変換できない。

    例: Zohoの「メルアポ」も「メール」もNotionでは「メール」になる。
    どちらへ戻すべきか決められないので、送らない。
    """
    table = zoho_outbound_field_names()

    assert "アクション種別" not in table["action"]
    assert "営業ステータス" not in table["project"]


def test_ambiguous_destination_is_excluded_rather_than_guessed() -> None:
    """送り先の候補が複数あり、ラベル完全一致で絞れないものは対象外にする。"""
    assert "議事録・録画リンク" not in zoho_outbound_field_names()["action"]


def test_exact_label_match_wins_when_multiple_candidates_exist() -> None:
    """候補が複数でも、ラベルがプロパティ名と完全一致するものがあればそれを採る。"""
    assert zoho_outbound_field_names()["project"]["契約日 / 予想契約日"] == "field50"


def test_translate_drops_unknown_properties_and_reports_them() -> None:
    translated, unmapped = translate_properties(
        zoho_outbound_field_names(), "project", {"案件名": "A社", "存在しない項目": "x"}
    )

    assert translated == {"Deal_Name": "A社"}
    assert unmapped == ["存在しない項目"]


def test_translate_refuses_everything_when_db_key_is_unknown() -> None:
    """db_keyが無いと変換表を引けない。素通しすると元の不具合に戻るので全部落とす。"""
    translated, unmapped = translate_properties(
        zoho_outbound_field_names(), None, {"案件名": "A社"}
    )

    assert translated == {}
    assert unmapped == ["案件名"]


def test_datetime_values_are_truncated_to_date() -> None:
    """Notionの日付は時刻付きで返ることがあり、kintone/Zohoの日付項目は受け付けない。"""
    translated, _unmapped = translate_properties(
        kintone_outbound_field_names(),
        "project",
        {"契約日 / 予想契約日": "2026-08-31T09:00:00.000+09:00"},
    )

    assert translated == {"日付_3": "2026-08-31"}


def test_free_text_starting_with_a_timestamp_is_not_truncated() -> None:
    """日付の切り詰めはDATE型にだけ効かせる。

    型を見ずに全文字列へ掛けると、たまたまISO日時から書き始められた自由記述が
    日付だけに切り詰められ、本文がサイレントに消える。
    """
    body = "2026-08-31T09:00 に先方から連絡あり。条件は据え置き。"
    translated, _unmapped = translate_properties(
        zoho_outbound_field_names(), "project", {"メモ": body}
    )

    assert translated == {"field70": body}


def test_labels_shared_by_multiple_zoho_fields_are_treated_as_ambiguous() -> None:
    """同じラベルが複数のapi_nameに付いているとき、先勝ちで畳み込まない。

    Deals の「作成日時」は `Created_Time` と `field42` の2つに割り当たっている。
    先勝ちにすると、読み取り専用のシステム項目へ書きに行く事故が起きうる。
    """
    from src.sync_engine.outbound_field_mapping import _zoho_api_names_by_label

    assert sorted(_zoho_api_names_by_label()["Deals"]["作成日時"]) == ["Created_Time", "field42"]


def test_empty_values_are_never_sent_outbound() -> None:
    """空値を送ると、項目名が正しくても外部の既存値が消える。

    「値の変更」と「値の削除」は別物として扱い、削除はいま伝播させない
    （消したいときにどう伝えるかが決まるまで、消さない側へ倒す）。
    """
    for empty in (None, "", "   ", [], {}):
        translated, unmapped = translate_properties(
            zoho_outbound_field_names(), "project", {"案件名": empty}
        )
        assert translated == {}, empty
        assert unmapped == ["案件名"], empty


def test_zero_and_false_are_treated_as_real_values() -> None:
    """0やFalseは「空」ではない。ここを取り違えると金額0が送れなくなる。"""
    translated, _unmapped = translate_properties(
        zoho_outbound_field_names(), "project", {"初期費用": 0}
    )

    assert translated == {"field": 0}
