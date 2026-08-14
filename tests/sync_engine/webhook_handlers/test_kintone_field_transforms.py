from __future__ import annotations

import pytest

from src.db_schema.registry import get_schema
from src.sync_engine.webhook_handlers.kintone_field_transforms import (
    KINTONE_FIELD_TRANSFORMS,
)


def test_all_mapped_notion_properties_exist_in_schema() -> None:
    # shirokuma-secレビューWARN対応（2026-08-14）: KINTONE_FIELD_TRANSFORMSのNotion
    # プロパティ名がタイポ等で実スキーマに存在しない場合、Dispatcher側のKeyErrorガードで
    # そのフィールドだけスキップされるため気づきにくい（動くように見えて実は無効）。
    # デプロイ前に機械的に検出できるようにする。
    for db_key, field_mapping in KINTONE_FIELD_TRANSFORMS.items():
        schema = get_schema(db_key)
        for kintone_field_code, (notion_property, _transform) in field_mapping.items():
            # get_property()は存在しない場合KeyErrorを送出する。
            schema.get_property(notion_property)


# 以下のフィールドコードは全て2026-08-14、実際のkintone REST API
# （GET /k/v1/app/form/fields.json）で検証済み（kintone_field_transforms.pyのモジュール
# docstring参照）。ラベルとコードが一致しないケースが複数あるため、キーには必ずコードを使う。


def test_project_status_field_normalizes_via_alias_table() -> None:
    # コード"ドロップダウン_2"、ラベル「契約進捗状況」。
    notion_property, transform = KINTONE_FIELD_TRANSFORMS["project"]["ドロップダウン_2"]

    assert notion_property == "営業ステータス"
    assert transform("商談中（B）") == "アポ"
    assert transform("契約済") == "契約"


def test_project_status_field_normalizes_half_width_brackets_too() -> None:
    # 2026-08-14、金沢さん指摘対応: kintone Webhook/REST APIの実データが半角括弧だった
    # 場合でも動くことをこのテーブル経由でも確認する（normalize_project_status自体の
    # テストはtests/migration/test_project_mapping.py参照）。
    _, transform = KINTONE_FIELD_TRANSFORMS["project"]["ドロップダウン_2"]

    assert transform("商談中(B)") == "アポ"


def test_project_status_field_raises_for_unmapped_value() -> None:
    _, transform = KINTONE_FIELD_TRANSFORMS["project"]["ドロップダウン_2"]

    with pytest.raises(ValueError):
        transform("存在しないステータス")


def test_project_billing_date_field_normalizes_to_iso() -> None:
    # コード"日付_3"、ラベル「課金開始予定日」。
    notion_property, transform = KINTONE_FIELD_TRANSFORMS["project"]["日付_3"]

    assert notion_property == "契約日 / 予想契約日"
    assert transform("2026-08-01") == "2026-08-01"


def test_project_monetary_fields_convert_to_float() -> None:
    # shirokuma-sec/obasan-qualityレビューBLOCKER対応（2026-08-14）: NUMBER型プロパティに
    # kintoneが返す文字列をそのまま渡すとNotion API側で拒否される。float変換が必要。
    # コード"初期費用_0"はラベル「提案料金（ランニング）」＝月額費用、
    # コード"初期費用"はラベル「提案料金（イニシャル）」＝初期費用（コードとラベルの
    # 対応が直感に反するため要注意、kintone_field_transforms.pyのコメント参照）。
    monthly_property, monthly_transform = KINTONE_FIELD_TRANSFORMS["project"]["初期費用_0"]
    initial_property, initial_transform = KINTONE_FIELD_TRANSFORMS["project"]["初期費用"]

    assert monthly_property == "月額費用"
    assert monthly_transform("50000") == 50000.0
    assert isinstance(monthly_transform("50000"), float)
    assert monthly_transform("") is None
    assert monthly_transform(None) is None

    assert initial_property == "初期費用"
    assert initial_transform("500000") == 500000.0


def test_client_master_prefecture_field_validates_against_schema_options() -> None:
    # shirokuma-secレビューWARN対応（2026-08-14）: zoho_field_transforms.pyの同一プロパティと
    # 同じくnormalize_prefectureで検証する（生値をそのまま渡さない）。コード==ラベル。
    notion_property, transform = KINTONE_FIELD_TRANSFORMS["client_master"]["都道府県名"]

    assert notion_property == "都道府県"
    assert transform("東京都") == "東京都"
    assert transform("存在しない県") is None


def test_client_master_customer_type_falls_back_instead_of_raising() -> None:
    _, transform = KINTONE_FIELD_TRANSFORMS["client_master"]["顧客種別"]

    # normalize_customer_typeは必須項目ではないためValueErrorではなくフォールバック値を返す
    # （src/migration/kintone_client_master.pyのdocstring参照）。
    assert transform("聞いたことのない業種") == "その他"


def test_client_master_direct_name_match_fields_pass_through() -> None:
    tel_property, tel_transform = KINTONE_FIELD_TRANSFORMS["client_master"]["TEL"]
    fax_property, fax_transform = KINTONE_FIELD_TRANSFORMS["client_master"]["FAX"]

    assert tel_property == "TEL"
    assert tel_transform("03-1234-5678") == "03-1234-5678"
    assert fax_property == "FAX"
    assert fax_transform("") is None


def test_client_master_name_and_address_fields_use_real_field_codes() -> None:
    # コード"顧客名"（ラベル「顧客名（法人・個人・施設）」）、コード"郵便番号"（ラベル「〒」）、
    # コード"住所"（ラベル「住所（市区町村以下を記載）」）— いずれもコード!=ラベル。
    name_property, name_transform = KINTONE_FIELD_TRANSFORMS["client_master"]["顧客名"]
    zip_property, zip_transform = KINTONE_FIELD_TRANSFORMS["client_master"]["郵便番号"]
    address_property, address_transform = KINTONE_FIELD_TRANSFORMS["client_master"]["住所"]

    assert name_property == "取引先名"
    assert name_transform("テスト商事") == "テスト商事"
    assert zip_property == "郵便番号"
    assert zip_transform("100-0001") == "100-0001"
    assert address_property == "住所"
    assert address_transform("千代田区1-1-1") == "千代田区1-1-1"


def test_action_type_field_normalizes_via_alias_table() -> None:
    # コード"actionContent"、ラベル「アクション内容」（2026-08-14、実際のkintone Webhook
    # 通知で確認済み）。
    notion_property, transform = KINTONE_FIELD_TRANSFORMS["action"]["actionContent"]

    assert notion_property == "アクション種別"
    assert transform("電話") == "テレアポ"
    assert transform("WEB商談") == "オンライン商談"


def test_action_comment_field_uses_real_field_code() -> None:
    # コード"comment"、ラベル「コメント」。
    notion_property, transform = KINTONE_FIELD_TRANSFORMS["action"]["comment"]

    assert notion_property == "履歴メモ"
    assert transform("折り返し予定") == "折り返し予定"
    assert transform("") is None


def test_relation_dependent_fields_are_intentionally_excluded() -> None:
    # リレーション解決が必要なフィールド（店舗名/対応者/担当者名/提案サービス等）や
    # 派生値フィールド（取引先マスターの営業ステータス等）は意図的にテーブルに含めない
    # （kintone_field_transforms.pyのモジュールdocstring参照）。コードは全て2026-08-14
    # 実APIで確認済み。
    assert "店舗名" not in KINTONE_FIELD_TRANSFORMS["project"]  # ラベル: 施設名（会社名）
    assert "cnctorMember" not in KINTONE_FIELD_TRANSFORMS["action"]  # ラベル: 対応者
    assert "toPerson" not in KINTONE_FIELD_TRANSFORMS["action"]  # ラベル: 担当者名
    assert "service" not in KINTONE_FIELD_TRANSFORMS["action"]  # ラベル: 提案サービス
    assert "client_name" not in KINTONE_FIELD_TRANSFORMS["action"]  # ラベル: 顧客名（法人・個人・施設）
    assert "nextActionDate" not in KINTONE_FIELD_TRANSFORMS["action"]  # ラベル: 次回アクション日
    assert "本部名" not in KINTONE_FIELD_TRANSFORMS["client_master"]  # コード==ラベル
