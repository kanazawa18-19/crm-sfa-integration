from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from scripts import verify_spreadsheet_backfill as audit
from src.db_schema.registry import get_schema
from src.sync_engine.id_mapping import IdMapping
from src.sync_engine.clients.spreadsheet_client import HttpSpreadsheetClient


def mapping(key, row=None):
    return IdMapping(notion_key=key, db_key="client_master", spreadsheet_row=row)


def test_collisions_and_shifted_rows_are_reported_without_choosing_owner():
    result = audit.inspect_mapping_rows(
        [mapping("a", 2), mapping("b", 2), mapping("c", 90)], ["b", "a", "c"]
    )
    assert result["row_collisions"] == [{"row": 2, "notion_keys": ["a", "b"]}]
    assert result["row_mismatches"] == [
        {"notion_key": "a", "saved_row": 2, "actual_rows": [3], "candidate_row": 3},
        {"notion_key": "c", "saved_row": 90, "actual_rows": [4], "candidate_row": 4},
    ]


def test_duplicate_and_missing_keys_have_no_repair_destination():
    result = audit.inspect_mapping_rows([mapping("a", 2), mapping("b", 3)], ["a", "", "a"])
    assert [item["candidate_row"] for item in result["row_mismatches"]] == [None, None]
    assert result["row_mismatches"][0]["actual_rows"] == [2, 4]


def test_unregistered_row_is_compatible_with_skip_id_mapping():
    result = audit.inspect_mapping_rows([mapping("a"), mapping("b", 3)], ["a", "b"])
    assert result == {"row_collisions": [], "row_mismatches": [], "unregistered_rows": 1}


@pytest.mark.parametrize("headers", [["名前"], [audit.SYNC_KEY_COLUMN, audit.SYNC_KEY_COLUMN]])
def test_missing_or_duplicate_column_never_writes(headers):
    client = HttpSpreadsheetClient(spreadsheet_id="synthetic", access_token="synthetic")
    response = requests.Response()
    response.status_code = 200
    import json
    response._content = json.dumps({"values": [headers]}).encode()
    client._request = Mock(return_value=response)
    with pytest.raises(ValueError, match="同期キー列"):
        audit._sync_key_cells(client, "取引先")
    assert [call.args[0] for call in client._request.call_args_list] == ["GET"]


def setup_cli(monkeypatch, mappings, cells):
    monkeypatch.setattr(audit, "_load_env", lambda: {})
    client = Mock()
    sheet = get_schema("client_master").spreadsheet_sheet_name
    client.count_rows.return_value = {sheet: len(cells) + 1}
    monkeypatch.setattr(audit, "build_spreadsheet_targets_by_db",
                        lambda: {"client_master": SimpleNamespace(_client=client)})
    store = Mock()
    store.list_by_db.return_value = mappings
    factory = Mock(return_value=store)
    monkeypatch.setattr(audit, "build_id_mapping_store", factory)
    monkeypatch.setattr(audit, "_sync_key_cells", lambda *_: cells)
    return factory


@pytest.mark.parametrize("mappings,cells", [
    ([mapping("a")], ["a", "orphan"]),
    ([mapping("a"), mapping("a")], ["a"]),
    ([mapping("a")], []),
    ([mapping("a")], ["a", "a"]),
    ([mapping("a", 8)], ["a"]),
])
def test_each_inconsistency_returns_failure(monkeypatch, capsys, mappings, cells):
    setup_cli(monkeypatch, mappings, cells)
    assert audit.main(["--db-keys", "client_master"]) == 1
    assert "✅" not in capsys.readouterr().out


def test_no_mapping_does_not_build_store_or_claim_full_success(monkeypatch, capsys):
    factory = setup_cli(monkeypatch, [], ["a"])
    assert audit.main(["--db-keys", "client_master", "--no-mapping"]) == 0
    factory.assert_not_called()
    assert "IDマッピングは未照合" in capsys.readouterr().out


def test_json_keeps_all_missing_keys_and_refuses_overwrite(monkeypatch, tmp_path):
    import json
    setup_cli(monkeypatch, [mapping(f"synthetic-{i}") for i in range(24)], [])
    path = tmp_path / "report.json"
    args = ["--db-keys", "client_master", "--report-json", str(path)]
    assert audit.main(args) == 1
    original = path.read_text()
    assert len(json.loads(original)["reports"][0]["missing_keys"]) == 24
    with pytest.raises(FileExistsError):
        audit.main(args)
    assert path.read_text() == original


def test_help_never_loads_credentials(monkeypatch):
    loader = Mock(side_effect=AssertionError("認証情報に触れた"))
    monkeypatch.setattr(audit, "_load_env", loader)
    with pytest.raises(SystemExit) as exc:
        audit.main(["--help"])
    assert exc.value.code == 0
    loader.assert_not_called()
