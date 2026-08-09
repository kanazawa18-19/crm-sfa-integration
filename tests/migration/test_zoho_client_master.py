import logging

import pytest

from src.migration.zoho_client_master import (
    normalize_company_name_basic,
    normalize_company_name_strong,
    normalize_prefecture,
    transform_zoho_client_master,
)


def test_normalize_company_name_basic_strips_whitespace() -> None:
    assert normalize_company_name_basic("  株式会社サンプル  ") == "株式会社サンプル"


def test_normalize_company_name_basic_empty_returns_empty_string() -> None:
    assert normalize_company_name_basic(None) == ""
    assert normalize_company_name_basic("") == ""


def test_normalize_company_name_strong_unifies_fullwidth_halfwidth() -> None:
    assert normalize_company_name_strong("ＰＬＡＺＡ　ＩＮ　ＫＡＮＫＵ　ＨＯＴＥＬ") == normalize_company_name_strong(
        "PLAZA IN KANKU HOTEL"
    )


def test_normalize_company_name_strong_removes_corporate_suffix() -> None:
    assert normalize_company_name_strong("株式会社ミドルウッド") == normalize_company_name_strong("ミドルウッド")


def test_normalize_company_name_strong_removes_internal_whitespace() -> None:
    assert normalize_company_name_strong("ストリングスホテル　名古屋") == normalize_company_name_strong(
        "ストリングスホテル名古屋"
    )


def test_normalize_prefecture_known_value_passthrough() -> None:
    assert normalize_prefecture("東京都") == "東京都"


def test_normalize_prefecture_empty_returns_none() -> None:
    assert normalize_prefecture("") is None
    assert normalize_prefecture(None) is None


def test_normalize_prefecture_unknown_value_falls_back_to_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """実データ確認済み(2026-08-10): 32,925件中1件のみ存在する明らかな入力ミス
    ("option1"等)を想定した回帰テスト。無言のフォールバックにならないようログへ残す。"""
    with caplog.at_level(logging.WARNING):
        result = normalize_prefecture("option1")

    assert result is None
    assert any("option1" in record.message for record in caplog.records)


def test_transform_zoho_client_master_maps_expected_fields() -> None:
    record = {
        "データID": "zcrm_123",
        "取引先名": "バンデホテルズ株式会社",
        "顧客種別": "宿泊施設",
        "郵便番号": "〒557-0044",
        "都道府県": "大阪府",
        "住所": "大阪府大阪市西成区玉出中2-1-24",
        "電話番号": "06-1234-5678",
        "Fax": "06-1234-5679",
    }

    result = transform_zoho_client_master(record)

    assert result == {
        "zoho_ID": "zcrm_123",
        "取引先名": "バンデホテルズ株式会社",
        "顧客種別": "宿泊施設",
        "郵便番号": "〒557-0044",
        "都道府県": "大阪府",
        "住所": "大阪府大阪市西成区玉出中2-1-24",
        "TEL": "06-1234-5678",
        "FAX": "06-1234-5679",
    }


def test_transform_zoho_client_master_missing_optional_fields_become_none() -> None:
    record = {"データID": "zcrm_456", "取引先名": "個人事業主A"}

    result = transform_zoho_client_master(record)

    assert result["郵便番号"] is None
    assert result["都道府県"] is None
    assert result["住所"] is None
    assert result["TEL"] is None
    assert result["FAX"] is None
    assert result["顧客種別"] is None


def test_transform_zoho_client_master_unmapped_customer_type_falls_back() -> None:
    """実データ確認済み: 「新規」「既存」はCLIENT_MASTER_SCHEMAの業種分類とは意味が異なる
    ステータス的な値のため、既存のnormalize_customer_typeのフォールバック方針
    （"その他"へ）がそのまま適用される。"""
    record = {"データID": "zcrm_789", "取引先名": "サンプル株式会社", "顧客種別": "新規"}

    result = transform_zoho_client_master(record)

    assert result["顧客種別"] == "その他"
