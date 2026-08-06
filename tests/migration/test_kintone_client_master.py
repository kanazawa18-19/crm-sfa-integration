import logging

import pytest

from src.migration.kintone_client_master import (
    derive_client_sales_status,
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


# --- derive_client_sales_status（BLOCKER1: 取引先マスターの営業ステータス導出） -----------


def test_derive_client_sales_status_no_projects_returns_not_approached() -> None:
    assert derive_client_sales_status([]) == "未アプローチ"


def test_derive_client_sales_status_any_contracted_project_wins() -> None:
    # 契約済案件が1件でもあれば、他が失注でも「契約」が最優先される。
    assert derive_client_sales_status(["失注", "契約済", "商談中(B)"]) == "契約"


def test_derive_client_sales_status_negotiation_in_progress() -> None:
    assert derive_client_sales_status(["商談中(B)"]) == "商談中"
    assert derive_client_sales_status(["商談中(C)"]) == "商談中"


def test_derive_client_sales_status_early_stage_maps_to_approaching() -> None:
    assert derive_client_sales_status(["初回接触"]) == "アプローチ中"
    assert derive_client_sales_status(["提案中"]) == "アプローチ中"
    assert derive_client_sales_status(["見積提出"]) == "アプローチ中"


def test_derive_client_sales_status_all_lost_or_cancelled() -> None:
    assert derive_client_sales_status(["失注"]) == "失注"
    assert derive_client_sales_status(["解約"]) == "失注"


def test_derive_client_sales_status_priority_order_negotiation_over_early_stage() -> None:
    assert derive_client_sales_status(["初回接触", "商談中(B)"]) == "商談中"


def test_derive_client_sales_status_unknown_status_falls_back_to_not_approached() -> None:
    """マッピング表に無い未知の案件ステータスのみの場合、安全側の「未アプローチ」に
    フォールバックする（normalize_customer_typeの未知値フォールバックと同種のパターン）。"""
    assert derive_client_sales_status(["謎のステータス"]) == "未アプローチ"


def test_derive_client_sales_status_unknown_status_logs_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """無言フォールバックにならないよう、未知の値を検知したらログへ元の値を残す。"""
    with caplog.at_level(logging.WARNING):
        derive_client_sales_status(["謎のステータス"])

    assert any("謎のステータス" in record.message for record in caplog.records)
