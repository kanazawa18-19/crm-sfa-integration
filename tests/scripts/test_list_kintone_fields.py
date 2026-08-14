"""scripts/list_kintone_fields.py の単体テスト。

実際のkintone本番APIへは一切到達させない（requests_mock）。読み取り専用スクリプトのため
書き込み系の検証は無い。
"""

from __future__ import annotations

from scripts.list_kintone_fields import fetch_fields, main, parse_args

DOMAIN = "example.cybozu.com"
FIELDS_URL = f"https://{DOMAIN}/k/v1/app/form/fields.json"


def test_fetch_fields_returns_properties(requests_mock) -> None:
    requests_mock.get(
        FIELDS_URL,
        json={
            "properties": {
                "actionContent": {"type": "DROP_DOWN", "label": "アクション内容"},
                "comment": {"type": "MULTI_LINE_TEXT", "label": "コメント"},
            }
        },
    )

    fields = fetch_fields(DOMAIN, "176", "token")

    assert fields["actionContent"]["label"] == "アクション内容"
    assert fields["comment"]["type"] == "MULTI_LINE_TEXT"
    assert requests_mock.last_request.headers["X-Cybozu-API-Token"] == "token"
    assert requests_mock.last_request.qs["app"] == ["176"]


def test_parse_args_resolves_db_key_choices() -> None:
    args = parse_args(["--db-key", "project", "--domain", DOMAIN])

    assert args.db_key == "project"
    assert args.domain == DOMAIN


def test_main_uses_db_key_env_vars(
    monkeypatch, requests_mock, capsys
) -> None:
    monkeypatch.setenv("KINTONE_APP_ID_ACTION", "176")
    monkeypatch.setenv("KINTONE_API_TOKEN_ACTION", "secret-token")
    requests_mock.get(
        FIELDS_URL,
        json={"properties": {"actionContent": {"type": "DROP_DOWN", "label": "アクション内容"}}},
    )

    main(["--db-key", "action", "--domain", DOMAIN])

    out = capsys.readouterr().out
    assert "actionContent" in out
    assert "secret-token" not in out


def test_main_skips_db_key_when_env_vars_missing(monkeypatch, capsys) -> None:
    monkeypatch.delenv("KINTONE_APP_ID_CLIENT", raising=False)
    monkeypatch.delenv("KINTONE_API_TOKEN_CLIENT", raising=False)

    main(["--db-key", "client_master", "--domain", DOMAIN])

    out = capsys.readouterr().out
    assert "スキップ" in out
