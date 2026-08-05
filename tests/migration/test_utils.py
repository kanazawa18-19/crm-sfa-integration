from src.migration._utils import parse_multi_value


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
