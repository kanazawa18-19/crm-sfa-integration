from __future__ import annotations

from src.api.notion_display import page_to_display_dict, parse_notion_property_for_display
from src.db_schema.registry import get_schema


# --- delegated types (rich_text/select/etc.) -----------------------------------------------


def test_delegates_title_to_existing_parser() -> None:
    prop = {"type": "title", "title": [{"plain_text": "MSA-PJ-001"}]}
    assert parse_notion_property_for_display(prop) == "MSA-PJ-001"


def test_parses_people_with_embedded_name() -> None:
    """回帰確認: peopleプロパティにはNotion側でnameが直接埋め込まれており、
    別途GET /v1/usersを叩かなくても表示名が取れる（実データではワークスペースメンバー一覧に
    現れないユーザーが存在することが判明したため、埋め込みnameを最優先で使う設計）。
    """
    prop = {
        "type": "people",
        "people": [
            {"id": "user-1", "name": "田中太郎"},
            {"id": "user-2", "name": None},
        ],
    }
    assert parse_notion_property_for_display(prop) == [
        {"id": "user-1", "name": "田中太郎"},
        {"id": "user-2", "name": None},
    ]


# --- rollup ----------------------------------------------------------------------------------


def test_parses_rollup_array_recursively() -> None:
    prop = {
        "type": "rollup",
        "rollup": {
            "type": "array",
            "array": [
                {"type": "select", "select": {"name": "商談中"}},
                {"type": "select", "select": {"name": "契約"}},
            ],
        },
    }
    assert parse_notion_property_for_display(prop) == ["商談中", "契約"]


def test_parses_rollup_number() -> None:
    prop = {"type": "rollup", "rollup": {"type": "number", "number": 42}}
    assert parse_notion_property_for_display(prop) == 42


def test_parses_rollup_date() -> None:
    prop = {
        "type": "rollup",
        "rollup": {"type": "date", "date": {"start": "2026-08-05"}},
    }
    assert parse_notion_property_for_display(prop) == {"start": "2026-08-05"}


# --- formula ----------------------------------------------------------------------------------


def test_parses_formula_string() -> None:
    prop = {"type": "formula", "formula": {"type": "string", "string": "OK"}}
    assert parse_notion_property_for_display(prop) == "OK"


def test_parses_formula_number() -> None:
    prop = {"type": "formula", "formula": {"type": "number", "number": 1.5}}
    assert parse_notion_property_for_display(prop) == 1.5


def test_parses_formula_boolean() -> None:
    prop = {"type": "formula", "formula": {"type": "boolean", "boolean": True}}
    assert parse_notion_property_for_display(prop) is True


def test_parses_formula_date() -> None:
    prop = {"type": "formula", "formula": {"type": "date", "date": {"start": "2026-08-05"}}}
    assert parse_notion_property_for_display(prop) == {"start": "2026-08-05"}


# --- unique_id ----------------------------------------------------------------------------------


def test_parses_unique_id_with_prefix() -> None:
    prop = {"type": "unique_id", "unique_id": {"prefix": "MSA-PJ-", "number": 12}}
    assert parse_notion_property_for_display(prop) == "MSA-PJ-12"


def test_parses_unique_id_without_prefix() -> None:
    prop = {"type": "unique_id", "unique_id": {"prefix": None, "number": 12}}
    assert parse_notion_property_for_display(prop) == "12"


# --- created_time / last_edited_time ------------------------------------------------------


def test_parses_created_time() -> None:
    prop = {"type": "created_time", "created_time": "2026-08-05T09:00:00.000Z"}
    assert parse_notion_property_for_display(prop) == "2026-08-05T09:00:00.000Z"


def test_parses_last_edited_time() -> None:
    prop = {"type": "last_edited_time", "last_edited_time": "2026-08-05T09:00:00.000Z"}
    assert parse_notion_property_for_display(prop) == "2026-08-05T09:00:00.000Z"


# --- created_by ----------------------------------------------------------------------------------


def test_parses_created_by_returns_id_only() -> None:
    prop = {"type": "created_by", "created_by": {"object": "user", "id": "user-1", "name": "田中太郎"}}
    assert parse_notion_property_for_display(prop) == {"id": "user-1", "name": None}


# --- files ----------------------------------------------------------------------------------


def test_parses_files_returns_name_list() -> None:
    prop = {
        "type": "files",
        "files": [{"name": "見積書.pdf"}, {"name": "契約書.pdf"}],
    }
    assert parse_notion_property_for_display(prop) == ["見積書.pdf", "契約書.pdf"]


# --- unknown types ----------------------------------------------------------------------------


def test_unknown_type_returns_none_instead_of_raising() -> None:
    prop = {"type": "some_future_type", "some_future_type": "x"}
    assert parse_notion_property_for_display(prop) is None


# --- page_to_display_dict ----------------------------------------------------------------------


def test_page_to_display_dict_converts_known_properties_and_includes_page_id() -> None:
    schema = get_schema("action")
    page = {
        "id": "page-1",
        "properties": {
            "商談回数・電話回数・メール回数（何回目）": {
                "type": "title",
                "title": [{"plain_text": "【電話】1回目"}],
            },
            "営業部アクションID": {"type": "unique_id", "unique_id": {"prefix": "SA-AC-", "number": 3}},
            "作成日時": {"type": "created_time", "created_time": "2026-08-05T09:00:00.000Z"},
            "作成者": {"type": "created_by", "created_by": {"id": "user-1", "name": "田中太郎"}},
            "担当営業": {
                "type": "rollup",
                "rollup": {
                    "type": "array",
                    "array": [
                        {
                            "type": "people",
                            "people": [{"id": "user-1", "name": "田中太郎"}],
                        }
                    ],
                },
            },
        },
    }

    result, skipped = page_to_display_dict(page, schema)

    assert result["notion_page_id"] == "page-1"
    assert result["商談回数・電話回数・メール回数（何回目）"] == "【電話】1回目"
    assert result["営業部アクションID"] == "SA-AC-3"
    assert result["作成日時"] == "2026-08-05T09:00:00.000Z"
    assert result["作成者"] == {"id": "user-1", "name": None}
    assert result["担当営業"] == [[{"id": "user-1", "name": "田中太郎"}]]
    assert skipped == set()


def test_page_to_display_dict_skips_unknown_property_and_returns_its_name(
    caplog,
) -> None:
    schema = get_schema("action")
    page = {
        "id": "page-1",
        "properties": {
            "存在しないプロパティ": {"type": "rich_text", "rich_text": [{"plain_text": "x"}]},
        },
    }

    with caplog.at_level("DEBUG"):
        result, skipped = page_to_display_dict(page, schema)

    assert "存在しないプロパティ" not in result
    assert skipped == {"存在しないプロパティ"}
    assert any("存在しないプロパティ" in record.getMessage() for record in caplog.records)
