from src.migration._utils import (
    extract_notion_page_id,
    normalize_date,
    parse_checkbox_columns,
    parse_multi_value,
)


def test_parse_multi_value_from_list() -> None:
    assert parse_multi_value(["リピッテ", " メイリー ", ""]) == ["リピッテ", "メイリー"]


def test_parse_multi_value_from_delimited_string() -> None:
    assert parse_multi_value("リピッテ、メイリー") == ["リピッテ", "メイリー"]
    assert parse_multi_value("リピッテ,メイリー") == ["リピッテ", "メイリー"]
    assert parse_multi_value("リピッテ，メイリー") == ["リピッテ", "メイリー"]


def test_parse_multi_value_empty() -> None:
    assert parse_multi_value(None) == []
    assert parse_multi_value("") == []
    assert parse_multi_value([]) == []


def test_parse_checkbox_columns_returns_checked_options() -> None:
    record = {
        "サービス（ランニング）[ホテラボ]": "",
        "サービス（ランニング）[メイリー]": "1",
        "サービス（ランニング）[リピッテ]": "1",
        "レコード番号": "123",
    }

    assert parse_checkbox_columns(record, prefix="サービス（ランニング）") == [
        "メイリー",
        "リピッテ",
    ]


def test_parse_checkbox_columns_returns_empty_list_when_no_matching_columns() -> None:
    assert parse_checkbox_columns({"レコード番号": "123"}, prefix="提案サービス") == []


def test_parse_checkbox_columns_returns_empty_list_when_all_unchecked() -> None:
    record = {"提案サービス[ホテラボ]": "", "提案サービス[メイリー]": ""}

    assert parse_checkbox_columns(record, prefix="提案サービス") == []


def test_parse_checkbox_columns_ignores_columns_with_different_prefix() -> None:
    record = {"サービス（イニシャル）[ホテラボ（初期）]": "1", "サービス（ランニング）[ホテラボ]": "1"}

    assert parse_checkbox_columns(record, prefix="サービス（ランニング）") == ["ホテラボ"]


def test_extract_notion_page_id_plain_url() -> None:
    text = "裾野セントラルホテル寿々木 (https://www.notion.so/5fbae3fd718f49e98eeb83aa10c880ea?pvs=21)"

    assert extract_notion_page_id(text) == "5fbae3fd718f49e98eeb83aa10c880ea"


def test_extract_notion_page_id_slugged_url() -> None:
    text = (
        "hotel la foresta（ホテル ラ フォレスタ） "
        "(https://www.notion.so/hotel-la-foresta-d78b68ec36fc49fbb49489e4b9229721?pvs=21)"
    )

    assert extract_notion_page_id(text) == "d78b68ec36fc49fbb49489e4b9229721"


def test_extract_notion_page_id_no_url_returns_none() -> None:
    assert extract_notion_page_id("プレーンテキスト") is None
    assert extract_notion_page_id("") is None
    assert extract_notion_page_id(None) is None


def test_normalize_date_passes_through_iso() -> None:
    assert normalize_date("2024-05-10") == "2024-05-10"


def test_normalize_date_passes_through_iso_with_time() -> None:
    assert normalize_date("2024-05-10T12:34:56.000Z") == "2024-05-10T12:34:56.000Z"


def test_normalize_date_converts_kanji_format() -> None:
    """Zoho実データ確認済み: '2024年5月10日'形式（本番移行のNotion API 400エラーの原因）。"""
    assert normalize_date("2024年5月10日") == "2024-05-10"
    assert normalize_date("2024年11月1日") == "2024-11-01"


def test_normalize_date_converts_slash_format() -> None:
    """kintone実データ確認済み: '2023/12/01'形式。"""
    assert normalize_date("2023/12/01") == "2023-12-01"
    assert normalize_date("2023/8/1") == "2023-08-01"


def test_normalize_date_unknown_format_returns_none() -> None:
    assert normalize_date("不明な値") is None
    assert normalize_date("May 10, 2024") is None


def test_normalize_date_rejects_out_of_range_month_or_day() -> None:
    """shirokuma-secレビューWARN対応の回帰テスト: 桁数だけ一致する暦上不正な値
    （13月・40日等）をISO"形式もどき"のまま通過させず、Noneへ落とすことを固定化する。"""
    assert normalize_date("2024年13月10日") is None
    assert normalize_date("2024年5月40日") is None
    assert normalize_date("2024/13/10") is None
    assert normalize_date("2024/5/40") is None


def test_normalize_date_iso_requires_full_date_prefix_not_trailing_garbage() -> None:
    """shirokuma-secレビューWARN対応の回帰テスト: 末尾アンカー無しの旧正規表現だと
    "2024-02-30xxxx"のような末尾にゴミが付いた文字列も素通ししていた。"""
    assert normalize_date("2024-02-30xxxx") is None


def test_normalize_date_empty_returns_none() -> None:
    assert normalize_date(None) is None
    assert normalize_date("") is None
    assert normalize_date("   ") is None
