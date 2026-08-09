from src.migration._utils import parse_checkbox_columns, parse_multi_value


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
