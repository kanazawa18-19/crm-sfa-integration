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


def test_choice_properties_are_sent_once_a_value_map_exists() -> None:
    """選択肢は、値の読み替えが用意できていれば送る（2026-08-31に追加）。

    読み替えはZohoの実際の選択肢を取り込み変換に通して反転して作る
    （`outbound_value_mapping.py`）。手で表を書かないので、取り込み側を直せば
    書き込み側も追随する。
    """
    table = zoho_outbound_field_names()

    assert table["project"]["営業ステータス"] == "Stage"
    assert table["product"]["課金形態"] == "field15"


def test_choice_properties_without_a_value_map_are_still_excluded() -> None:
    """読み替えが作れない選択肢は引き続き対象外。当てずっぽうで送らない。"""
    table = zoho_outbound_field_names()

    # 「確度」はZoho側に対応する選択肢項目が無い。
    assert "確度" not in table["project"]


def test_ambiguous_destination_is_excluded_rather_than_guessed() -> None:
    """送り先の候補が複数あり、決め手が無いものは対象外にする。"""
    from src.sync_engine.outbound_field_mapping import _choose_unique_outbound_target

    assert (
        _choose_unique_outbound_target("なにか", [("ラベルA", "field1"), ("ラベルB", "field2")])
        is None
    )


def test_destination_decided_from_real_data_is_used() -> None:
    """候補が複数でも、実データを見て決めたものは登録する。

    「議事録・録画リンク」の候補は Zoho の `Notta` と `field21`（録画・音声ファイル）。
    CustomModule2の200件を実測したところ `Notta` に1件、`field21` は0件だったので
    使われている方へ書く（2026-08-31）。
    """
    assert zoho_outbound_field_names()["action"]["議事録・録画リンク"] == "Notta"


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
