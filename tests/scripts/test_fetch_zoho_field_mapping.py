"""scripts/fetch_zoho_field_mapping.py の単体テスト。

実際のZoho本番APIへは一切到達させない（requests_mock）。config/zoho_field_mapping.json
本体も書き換えず、常にtmp_path配下の別ファイルを対象に検証する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.fetch_zoho_field_mapping import (
    diff_module_mapping,
    fetch_module_field_mapping,
    load_full_mapping,
    main,
    parse_args,
)
from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError

TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
SETTINGS_API_BASE_URL = "https://www.zohoapis.mock/crm/v3"
SETTINGS_URL = f"{SETTINGS_API_BASE_URL}/settings/fields"


@pytest.fixture(autouse=True)
def _zoho_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_CLIENT_ID", "cid")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "rtoken")
    monkeypatch.delenv("ZOHO_ACCOUNTS_BASE_URL", raising=False)
    monkeypatch.delenv("ZOHO_API_BASE_URL", raising=False)


def _mock_token(requests_mock) -> None:
    requests_mock.post(TOKEN_URL, json={"access_token": "access-token-1", "expires_in": 3600})


# --- fetch_module_field_mapping ----------------------------------------------------------------


def test_fetch_module_field_mapping_parses_api_name_and_label(requests_mock) -> None:
    _mock_token(requests_mock)
    requests_mock.get(
        SETTINGS_URL,
        json={
            "fields": [
                {"api_name": "field71", "field_label": "営業ステータス", "data_type": "picklist"},
                {"api_name": "Deal_Name", "field_label": "案件名", "data_type": "text"},
                {"api_name": "no_label_field"},  # field_labelが無い異常なエントリはスキップする
            ]
        },
    )
    client = HttpZohoClient()

    mapping = fetch_module_field_mapping(
        client, module="Deals", settings_api_base_url=SETTINGS_API_BASE_URL
    )

    assert mapping == {"field71": "営業ステータス", "Deal_Name": "案件名"}
    settings_calls = [
        req for req in requests_mock.request_history if req.url.startswith(SETTINGS_URL)
    ]
    assert len(settings_calls) == 1
    assert "module=Deals" in settings_calls[0].url


def test_fetch_module_field_mapping_raises_on_http_error(requests_mock) -> None:
    _mock_token(requests_mock)
    requests_mock.get(SETTINGS_URL, status_code=400, json={"message": "invalid module"})
    client = HttpZohoClient()

    with pytest.raises(ZohoApiError):
        fetch_module_field_mapping(
            client, module="Deals", settings_api_base_url=SETTINGS_API_BASE_URL
        )


def test_fetch_module_field_mapping_raises_when_fields_array_missing(requests_mock) -> None:
    _mock_token(requests_mock)
    requests_mock.get(SETTINGS_URL, json={"unexpected": "shape"})
    client = HttpZohoClient()

    with pytest.raises(ZohoApiError):
        fetch_module_field_mapping(
            client, module="Deals", settings_api_base_url=SETTINGS_API_BASE_URL
        )


# --- diff_module_mapping ------------------------------------------------------------------------


def test_diff_module_mapping_detects_added_removed_and_changed() -> None:
    old = {"field1": "変わらない", "field2": "削除される", "field3": "旧ラベル"}
    new = {"field1": "変わらない", "field3": "新ラベル", "field4": "新規"}

    diff = diff_module_mapping(old, new)

    assert diff["added"] == {"field4": "新規"}
    assert diff["removed"] == {"field2": "削除される"}
    assert diff["changed"] == {"field3": ("旧ラベル", "新ラベル")}
    assert diff["unchanged_count"] == 1


def test_diff_module_mapping_no_changes() -> None:
    mapping = {"field1": "ラベル"}

    diff = diff_module_mapping(mapping, dict(mapping))

    assert diff == {"added": {}, "removed": {}, "changed": {}, "unchanged_count": 1}


# --- load_full_mapping ---------------------------------------------------------------------------


def test_load_full_mapping_returns_empty_dict_when_file_missing(tmp_path: Path) -> None:
    assert load_full_mapping(tmp_path / "does_not_exist.json") == {}


def test_load_full_mapping_reads_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "zoho_field_mapping.json"
    path.write_text(json.dumps({"Deals": {"field71": "営業ステータス"}}), encoding="utf-8")

    assert load_full_mapping(path) == {"Deals": {"field71": "営業ステータス"}}


# --- parse_args -----------------------------------------------------------------------------------


def test_parse_args_defaults_to_deals_module() -> None:
    args = parse_args([])

    assert args.module == "Deals"


# --- main(): 対象モジュールのみ更新し、他モジュールを壊さない ------------------------------------


def test_main_updates_only_target_module_without_clobbering_others(
    requests_mock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    mapping_path = tmp_path / "zoho_field_mapping.json"
    mapping_path.write_text(
        json.dumps(
            {
                "Deals": {"field71": "旧ラベル", "field99": "廃止されたフィールド"},
                "Contacts": {"field1": "既存の別モジュール"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _mock_token(requests_mock)
    requests_mock.get(
        SETTINGS_URL,
        json={
            "fields": [
                {"api_name": "field71", "field_label": "営業ステータス"},
                {"api_name": "field200", "field_label": "新規フィールド"},
            ]
        },
    )

    main(
        [
            "--module",
            "Deals",
            "--path",
            str(mapping_path),
            "--api-base-url",
            SETTINGS_API_BASE_URL,
        ]
    )

    updated = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert updated["Deals"] == {"field71": "営業ステータス", "field200": "新規フィールド"}
    assert updated["Contacts"] == {"field1": "既存の別モジュール"}  # 他モジュールは無傷

    captured = capsys.readouterr()
    assert "追加" in captured.out
    assert "field200: 新規フィールド" in captured.out
    assert "削除" in captured.out
    assert "field99: 廃止されたフィールド" in captured.out
    assert "ラベル変更" in captured.out
    assert "field71: 旧ラベル -> 営業ステータス" in captured.out


def test_main_creates_new_mapping_file_when_none_exists(requests_mock, tmp_path: Path) -> None:
    mapping_path = tmp_path / "zoho_field_mapping.json"
    _mock_token(requests_mock)
    requests_mock.get(
        SETTINGS_URL, json={"fields": [{"api_name": "field71", "field_label": "営業ステータス"}]}
    )

    main(
        [
            "--module",
            "Deals",
            "--path",
            str(mapping_path),
            "--api-base-url",
            SETTINGS_API_BASE_URL,
        ]
    )

    assert json.loads(mapping_path.read_text(encoding="utf-8")) == {
        "Deals": {"field71": "営業ステータス"}
    }


def test_main_adds_new_module_section_alongside_existing_ones(
    requests_mock, tmp_path: Path
) -> None:
    mapping_path = tmp_path / "zoho_field_mapping.json"
    mapping_path.write_text(
        json.dumps({"Deals": {"field71": "営業ステータス"}}, ensure_ascii=False), encoding="utf-8"
    )
    _mock_token(requests_mock)
    requests_mock.get(
        SETTINGS_URL, json={"fields": [{"api_name": "Account_Name", "field_label": "取引先名"}]}
    )

    main(
        [
            "--module",
            "Accounts",
            "--path",
            str(mapping_path),
            "--api-base-url",
            SETTINGS_API_BASE_URL,
        ]
    )

    updated = json.loads(mapping_path.read_text(encoding="utf-8"))
    assert updated["Deals"] == {"field71": "営業ステータス"}
    assert updated["Accounts"] == {"Account_Name": "取引先名"}
