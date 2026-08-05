import pytest

from src.migration.project_mapping import normalize_project_status, transform_kintone_project


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("契約済", "契約済"),
        ("商談中(B)", "商談中(B)"),
        ("商談中(C)", "商談中(C)"),
        ("商談中（B）", "商談中(B)"),
        ("商談中（C）", "商談中(C)"),
        ("失注", "失注"),
        ("初回接触", "初回接触"),
        (" 提案中 ", "提案中"),
        ("見積提出", "見積提出"),
        (" 見積提出 ", "見積提出"),
        ("解約", "解約"),
        (" 解約 ", "解約"),
    ],
)
def test_normalize_project_status_known_values(raw: str, expected: str) -> None:
    assert normalize_project_status(raw) == expected


def test_normalize_project_status_covers_all_schema_options() -> None:
    """仕様書03節の営業ステータス全8値を網羅していることを保証する回帰テスト。"""
    from src.db_schema.project import PROJECT_SCHEMA

    valid_options = PROJECT_SCHEMA.get_property("営業ステータス").options
    assert len(valid_options) == 8
    for option in valid_options:
        assert normalize_project_status(option) == option


def test_normalize_project_status_unknown_value_raises() -> None:
    with pytest.raises(ValueError):
        normalize_project_status("謎のステータス")


def test_normalize_project_status_none_raises_instead_of_attribute_error() -> None:
    """kintoneの空欄フィールドはNoneで返ってくることがあるため、AttributeErrorにならず
    ValueErrorとして扱われることを確認する。"""
    with pytest.raises(ValueError):
        normalize_project_status(None)


def test_transform_kintone_project_status_field_none_raises_value_error() -> None:
    record = {
        "レコード番号": "3003",
        "施設名（会社名）": "株式会社サンプル3",
        "契約進捗状況": None,
    }

    with pytest.raises(ValueError):
        transform_kintone_project(record)


def test_transform_kintone_project_contracted_sets_contract_date() -> None:
    record = {
        "レコード番号": "3001",
        "施設名（会社名）": "株式会社サンプル",
        "契約進捗状況": "契約済",
        "課金開始予定日": "2026-09-01",
        "サービス（ショット）": "リピッテ、メイリー",
        "提案料金（ランニング）": "50000",
        "提案料金（イニシャル）": "100000",
    }

    result = transform_kintone_project(record)

    assert result["営業ステータス"] == "契約済"
    assert result["契約日"] == "2026-09-01"
    assert result["予想契約日"] is None
    assert result["_サービス名リスト"] == ["リピッテ", "メイリー"]
    assert result["月額費用（ランニング）"] == "50000"
    assert result["初期費用（イニシャル）"] == "100000"
    assert result["_取引先名"] == "株式会社サンプル"
    assert result["kintone_ID"] == "3001"


def test_transform_kintone_project_in_progress_sets_expected_date() -> None:
    record = {
        "レコード番号": "3002",
        "施設名（会社名）": "株式会社サンプル2",
        "契約進捗状況": "商談中(B)",
        "課金開始予定日": "2026-10-01",
    }

    result = transform_kintone_project(record)

    assert result["契約日"] is None
    assert result["予想契約日"] == "2026-10-01"
    assert result["_サービス名リスト"] == []
