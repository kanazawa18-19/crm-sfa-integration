from src.migration._utils import extract_notion_page_id, parse_checkbox_columns, parse_multi_value


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
