import logging

import pytest

from src.migration.kintone_client_master import (
    extract_chain_name,
    normalize_customer_type,
    transform_client_master,
)


def test_normalize_customer_type_known_value_passthrough() -> None:
    assert normalize_customer_type("ホテル・旅館") == "ホテル・旅館"


def test_normalize_customer_type_strips_whitespace() -> None:
    assert normalize_customer_type("  飲食 ") == "飲食"


def test_normalize_customer_type_unknown_value_falls_back() -> None:
    assert normalize_customer_type("学習塾") == "その他"


def test_normalize_customer_type_unknown_value_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """無言フォールバックにならないよう、未知の値を検知したらログへ元の値を残す。"""
    with caplog.at_level(logging.WARNING):
        normalize_customer_type("学習塾")

    assert any("学習塾" in record.message for record in caplog.records)


def test_normalize_customer_type_empty_returns_none() -> None:
    assert normalize_customer_type("") is None
    assert normalize_customer_type(None) is None


def test_transform_client_master_maps_expected_fields() -> None:
    record = {
        "レコード番号": "1001",
        "顧客名（法人・個人・施設）": "株式会社サンプル",
        "顧客種別": "ホテル・旅館",
        "〒": "100-0001",
        "都道府県": "東京都",
        "住所": "千代田区1-1-1",
        "TEL": "03-1234-5678",
        "FAX": "03-1234-5679",
    }

    result = transform_client_master(record)

    assert result == {
        "kintone_ID": "1001",
        "取引先名": "株式会社サンプル",
        "顧客種別": "ホテル・旅館",
        "郵便番号": "100-0001",
        "都道府県": "東京都",
        "住所": "千代田区1-1-1",
        "TEL": "03-1234-5678",
        "FAX": "03-1234-5679",
    }


def test_transform_client_master_missing_optional_fields_become_none() -> None:
    record = {"レコード番号": "1002", "顧客名（法人・個人・施設）": "個人事業主A"}

    result = transform_client_master(record)

    assert result["郵便番号"] is None
    assert result["TEL"] is None
    assert result["顧客種別"] is None


def test_extract_chain_name_returns_stripped_name() -> None:
    assert extract_chain_name({"本部名": " サンプルチェーン本部 "}) == "サンプルチェーン本部"


def test_extract_chain_name_returns_none_when_blank() -> None:
    assert extract_chain_name({"本部名": "  "}) is None
    assert extract_chain_name({}) is None
