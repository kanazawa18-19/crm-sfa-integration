import logging

import pytest

from src.migration.kintone_client_master import (
    derive_client_sales_status,
    extract_chain_name,
    normalize_customer_type,
    remap_duplicate_contact_columns,
    transform_client_master,
)


def test_normalize_customer_type_known_value_passthrough() -> None:
    assert normalize_customer_type("ホテル・旅館") == "ホテル・旅館"


def test_normalize_customer_type_strips_whitespace() -> None:
    assert normalize_customer_type("  飲食店 ") == "飲食店"


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
    """実データ回帰確認: 「都道府県」「住所」はkintone実データ側の列名
    （「都道府県名」「住所（市区町村以下を記載）」）を参照する。"""
    record = {
        "レコード番号": "1001",
        "顧客名（法人・個人・施設）": "株式会社サンプル",
        "顧客種別": "ホテル・旅館",
        "〒": "100-0001",
        "都道府県名": "東京都",
        "住所（市区町村以下を記載）": "千代田区1-1-1",
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


# --- remap_duplicate_contact_columns（担当者1〜3人分の重複列の一意化） -------------------


def _make_header() -> list[str]:
    """実データ確認済みの取引先マスタCSVヘッダー（41列）を組み立てる。"""
    header = [""] * 41
    header[0] = "顧客名（法人・個人・施設）"
    header[7] = "部署"
    header[8] = "担当者名2"
    header[9] = "レコード番号"
    header[20] = "メールアドレス"
    header[21] = "担当者名"
    header[22] = "部署"
    header[23] = "役職"
    header[24] = "携帯番号"
    header[26] = "部署"
    header[28] = "メールアドレス"
    header[29] = "役職"
    header[30] = "携帯番号"
    header[31] = "担当者名3"
    header[33] = "メールアドレス"
    header[39] = "携帯番号"
    header[40] = "役職"
    return header


def _make_row(values: dict[int, str]) -> list[str]:
    row = [""] * 41
    for index, value in values.items():
        row[index] = value
    return row


def test_remap_duplicate_contact_columns_extracts_all_three_contacts() -> None:
    """実データ回帰確認: 「部署」「役職」「携帯番号」「メールアドレス」列が担当者1〜3人分、
    同名列として重複エクスポートされていても、1〜3人目それぞれの値を取り出せる。"""
    header = _make_header()
    row = _make_row(
        {
            0: "株式会社サンプル",
            9: "1001",
            20: "ichiro@example.com",
            21: "田中一郎",
            22: "営業部",
            23: "部長",
            24: "090-1111-1111",
            8: "鈴木二郎",
            26: "経理部",
            28: "jiro@example.com",
            29: "係長",
            30: "090-2222-2222",
            31: "佐藤三郎",
            33: "saburo@example.com",
            39: "090-3333-3333",
            40: "主任",
        }
    )

    result = remap_duplicate_contact_columns(header, row)

    assert result["顧客名（法人・個人・施設）"] == "株式会社サンプル"
    assert result["レコード番号"] == "1001"
    assert result["担当者名1"] == "田中一郎"
    assert result["部署1"] == "営業部"
    assert result["役職1"] == "部長"
    assert result["携帯1"] == "090-1111-1111"
    assert result["メール1"] == "ichiro@example.com"
    assert result["担当者名2"] == "鈴木二郎"
    assert result["部署2"] == "経理部"
    assert result["役職2"] == "係長"
    assert result["携帯2"] == "090-2222-2222"
    assert result["メール2"] == "jiro@example.com"
    assert result["担当者名3"] == "佐藤三郎"
    assert result["役職3"] == "主任"
    assert result["携帯3"] == "090-3333-3333"
    assert result["メール3"] == "saburo@example.com"


def test_remap_duplicate_contact_columns_raises_when_header_layout_unexpected() -> None:
    """列インデックスがハードコードされているため、想定と異なるヘッダーが来たら
    静かに誤ったデータを拾わず明示的にエラーにする。"""
    header = _make_header()
    header[21] = "違うラベル"

    with pytest.raises(ValueError, match="列レイアウトが想定と異なります"):
        remap_duplicate_contact_columns(header, _make_row({}))
