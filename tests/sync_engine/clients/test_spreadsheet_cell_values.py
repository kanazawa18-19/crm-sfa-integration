"""セル値の変換（`_to_cell_value`、2026-08-31）。

Sheetsはセルにスカラーしか受け付けない。リレーション（NotionページIDの配列）を
そのまま渡すと400になり、**その行だけでなくバッチ全体が失敗する**。
バックフィルを`--apply`して初めて出た（試算では気づけなかった）。
"""

from __future__ import annotations

from src.sync_engine.clients.spreadsheet_client import _to_cell_value


def test_relation_lists_become_a_joined_string() -> None:
    assert _to_cell_value(["page-a", "page-b"]) == "page-a, page-b"


def test_empty_list_becomes_an_empty_cell() -> None:
    assert _to_cell_value([]) == ""


def test_lookup_dict_prefers_the_readable_name() -> None:
    assert _to_cell_value({"name": "ホテルユクエスタ旭橋", "id": "2233"}) == "ホテルユクエスタ旭橋"


def test_dict_without_a_name_is_kept_as_json() -> None:
    assert _to_cell_value({"id": "2233"}) == '{"id": "2233"}'


def test_none_becomes_an_empty_cell() -> None:
    assert _to_cell_value(None) == ""


def test_scalars_pass_through_unchanged() -> None:
    assert _to_cell_value("A社") == "A社"
    assert _to_cell_value(0) == 0
    assert _to_cell_value(1500.5) == 1500.5
    assert _to_cell_value(False) is False
