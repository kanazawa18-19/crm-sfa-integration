"""scripts/register_zoho_webhook.py の単体テスト。

実際のZoho本番APIへは一切到達させない。requests_mockで未登録URLへのリクエストは
NoMockAddress例外となるため、dry-run（--yes無し）時にネットワーク呼び出しが一切
発生しないことも同時に検証できる。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.register_zoho_webhook as register_zoho_webhook
from scripts.register_zoho_webhook import (
    build_watch_payload,
    compute_channel_expiry,
    main,
    parse_args,
    register_or_renew_watch,
)
from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError

TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
WATCH_API_BASE_URL = "https://www.zohoapis.mock/crm/v3"
WATCH_URL = f"{WATCH_API_BASE_URL}/actions/watch"


@pytest.fixture(autouse=True)
def _zoho_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOHO_CLIENT_ID", "cid")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ZOHO_REFRESH_TOKEN", "rtoken")
    monkeypatch.delenv("ZOHO_ACCOUNTS_BASE_URL", raising=False)
    monkeypatch.delenv("ZOHO_API_BASE_URL", raising=False)
    # BLOCKER3のテストが決定的になるよう、ホスト環境のZOHO_WEBHOOK_SECRETに依存しない。
    monkeypatch.delenv("ZOHO_WEBHOOK_SECRET", raising=False)


@pytest.fixture(autouse=True)
def _isolated_channel_state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """WARN4: 実行のたびにリポジトリ直下の.zoho_watch_channel.jsonを汚さないよう、
    テスト中は保存先をtmp_path配下へ差し替える。"""
    path = tmp_path / ".zoho_watch_channel.json"
    monkeypatch.setattr(register_zoho_webhook, "_CHANNEL_STATE_PATH", path)
    return path


def _mock_token(requests_mock) -> None:
    requests_mock.post(TOKEN_URL, json={"access_token": "access-token-1", "expires_in": 3600})


# --- compute_channel_expiry / build_watch_payload -------------------------------------------


def test_compute_channel_expiry_adds_days() -> None:
    now = datetime(2026, 8, 12, 9, 0, 0, tzinfo=timezone.utc)

    expiry = compute_channel_expiry(7, now=now)

    assert expiry == "2026-08-19T09:00:00+00:00"


def test_build_watch_payload_includes_module_and_notify_url() -> None:
    payload = build_watch_payload(
        channel_id="123",
        module="Deals",
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token="secret-token",
    )

    assert payload == {
        "watch": [
            {
                "channel_id": "123",
                "events": [{"channel_id": "123", "module": "Deals"}],
                "channel_expiry": "2026-08-19T09:00:00+00:00",
                "notify_url": "https://example.com/api/webhooks/zoho",
                "token": "secret-token",
            }
        ]
    }


def test_build_watch_payload_omits_token_when_not_provided() -> None:
    payload = build_watch_payload(
        channel_id="123",
        module="Deals",
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token=None,
    )

    assert "token" not in payload["watch"][0]


# --- parse_args -------------------------------------------------------------------------------


def test_parse_args_defaults_to_fresh_registration() -> None:
    args = parse_args(["--base-url", "https://example.com"])

    assert args.module == "Deals"
    assert args.channel_id is None
    assert args.expiry_days == 7
    assert args.yes is False


def test_parse_args_with_channel_id_for_renewal() -> None:
    args = parse_args(["--base-url", "https://example.com", "--channel-id", "999"])

    assert args.channel_id == "999"


# --- register_or_renew_watch ------------------------------------------------------------------


def test_register_or_renew_watch_posts_for_fresh_registration(requests_mock) -> None:
    _mock_token(requests_mock)
    requests_mock.post(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})
    client = HttpZohoClient()
    payload = build_watch_payload(
        channel_id="123",
        module="Deals",
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token=None,
    )

    result = register_or_renew_watch(
        client, watch_api_base_url=WATCH_API_BASE_URL, payload=payload, is_renewal=False
    )

    assert result == {"watch": [{"channel_id": "123", "status": "success"}]}
    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1
    assert watch_calls[0].method == "POST"
    assert watch_calls[0].json() == payload


def test_register_or_renew_watch_puts_for_renewal(requests_mock) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})
    client = HttpZohoClient()
    payload = build_watch_payload(
        channel_id="123",
        module="Deals",
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token=None,
    )

    register_or_renew_watch(
        client, watch_api_base_url=WATCH_API_BASE_URL, payload=payload, is_renewal=True
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1
    assert watch_calls[0].method == "PUT"


def test_register_or_renew_watch_raises_on_non_success_entry(requests_mock) -> None:
    _mock_token(requests_mock)
    requests_mock.post(
        WATCH_URL,
        json={"watch": [{"status": "error", "code": "INVALID_DATA", "message": "bad channel_id"}]},
    )
    client = HttpZohoClient()
    payload = build_watch_payload(
        channel_id="123",
        module="Deals",
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token=None,
    )

    with pytest.raises(ZohoApiError):
        register_or_renew_watch(
            client, watch_api_base_url=WATCH_API_BASE_URL, payload=payload, is_renewal=False
        )


def test_register_or_renew_watch_raises_on_http_error(requests_mock) -> None:
    _mock_token(requests_mock)
    requests_mock.post(WATCH_URL, status_code=400, json={"message": "invalid request"})
    client = HttpZohoClient()
    payload = build_watch_payload(
        channel_id="123",
        module="Deals",
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token=None,
    )

    with pytest.raises(ZohoApiError):
        register_or_renew_watch(
            client, watch_api_base_url=WATCH_API_BASE_URL, payload=payload, is_renewal=False
        )


# --- main(): dry-run既定・--yesで初めて実APIを叩く ---------------------------------------------


def test_main_without_yes_does_not_call_any_api(requests_mock, capsys: pytest.CaptureFixture[str]) -> None:
    """requests_mockに何も登録しない状態で実行し、ネットワーク呼び出しが一切無いことを
    NoMockAddress例外が発生しないこと（=呼び出し自体が起きないこと）で検証する。"""
    main(["--base-url", "https://example.com", "--watch-api-base-url", WATCH_API_BASE_URL])

    captured = capsys.readouterr()
    assert "dry-run" in captured.out
    assert len(requests_mock.request_history) == 0


def test_main_with_yes_calls_watch_api(requests_mock, capsys: pytest.CaptureFixture[str]) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})

    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--channel-id",
            "123",
            "--module",
            "Deals",
            "--token",
            "test-token",
            "--yes",
        ]
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1
    sent_body = watch_calls[0].json()
    assert sent_body["watch"][0]["channel_id"] == "123"
    assert sent_body["watch"][0]["events"] == [{"channel_id": "123", "module": "Deals"}]
    assert sent_body["watch"][0]["notify_url"] == "https://example.com/api/webhooks/zoho"
    captured = capsys.readouterr()
    assert "完了しました" in captured.out


def test_main_uses_zoho_webhook_secret_env_as_default_token(
    requests_mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZOHO_WEBHOOK_SECRET", "env-secret")
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})

    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--channel-id",
            "123",
            "--yes",
        ]
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert watch_calls[0].json()["watch"][0]["token"] == "env-secret"


# --- BLOCKER1: 実tokenがstdoutへ平文で漏れないこと -----------------------------------------


def test_main_dry_run_never_prints_raw_token(capsys: pytest.CaptureFixture[str]) -> None:
    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--token",
            "super-secret-value",
        ]
    )

    captured = capsys.readouterr()
    assert "super-secret-value" not in captured.out
    assert "***REDACTED***" in captured.out


def test_main_with_yes_never_prints_raw_token(
    requests_mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """dry-run表示だけでなく、--yes実行時のAPIレスポンス表示（tokenをエコーバックしてくる
    ケースを想定したモック）でも実tokenが平文で出力されないことを確認する。"""
    _mock_token(requests_mock)
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {"channel_id": "123", "status": "success", "token": "super-secret-value"}
            ]
        },
    )

    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--channel-id",
            "123",
            "--token",
            "super-secret-value",
            "--yes",
        ]
    )

    captured = capsys.readouterr()
    assert "super-secret-value" not in captured.out


# --- BLOCKER3: --yesで空tokenの登録を事故らせない --------------------------------------------


def test_main_with_yes_and_empty_token_refuses_without_calling_api(
    requests_mock, capsys: pytest.CaptureFixture[str]
) -> None:
    """requests_mockに何も登録しない状態で実行し、実際にAPI呼び出しが一切発生しないことを
    NoMockAddress例外が起きないこと（=呼ばれていないこと）で確認する。"""
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--base-url",
                "https://example.com",
                "--watch-api-base-url",
                WATCH_API_BASE_URL,
                "--channel-id",
                "123",
                "--yes",
            ]
        )

    assert exc_info.value.code != 0
    assert len(requests_mock.request_history) == 0


def test_main_with_yes_and_allow_empty_token_proceeds(requests_mock) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})

    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--channel-id",
            "123",
            "--yes",
            "--allow-empty-token",
        ]
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1


def test_main_with_yes_and_explicit_token_does_not_require_allow_empty_token(
    requests_mock,
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})

    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--channel-id",
            "123",
            "--token",
            "explicit-token",
            "--yes",
        ]
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1


# --- WARN4: channel_id/channel_expiryの永続化と延長時の読み戻し -------------------------------


def test_main_with_yes_persists_channel_state_and_prints_grepable_line(
    requests_mock,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})
    state_path = tmp_path / ".zoho_watch_channel.json"
    monkeypatch.setattr(register_zoho_webhook, "_CHANNEL_STATE_PATH", state_path)

    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--channel-id",
            "123",
            "--expiry-days",
            "7",
            "--token",
            "test-token",
            "--yes",
        ]
    )

    saved = json.loads(state_path.read_text())
    assert saved["channel_id"] == "123"
    assert "channel_expiry" in saved
    captured = capsys.readouterr()
    assert "ZOHO_WATCH_CHANNEL_ID=123" in captured.out
    assert f"ZOHO_WATCH_EXPIRY={saved['channel_expiry']}" in captured.out


def test_main_without_channel_id_reads_persisted_channel_id_as_default(
    requests_mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--channel-id省略時、前回`--yes`実行時に保存されたchannel_idを延長対象として使う。"""
    state_path = tmp_path / ".zoho_watch_channel.json"
    state_path.write_text(json.dumps({"channel_id": "999", "channel_expiry": "2026-08-19T00:00:00+00:00"}))
    monkeypatch.setattr(register_zoho_webhook, "_CHANNEL_STATE_PATH", state_path)
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "999", "status": "success"}]})

    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--token",
            "test-token",
            "--yes",
        ]
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1
    assert watch_calls[0].method == "PUT"  # 延長（renewal）として扱われる
    assert watch_calls[0].json()["watch"][0]["channel_id"] == "999"


def test_main_without_channel_id_generates_new_one_when_no_state_file(
    requests_mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """状態ファイルが存在しない場合は従来どおり新規登録（POST）になる。"""
    monkeypatch.setattr(register_zoho_webhook, "_CHANNEL_STATE_PATH", tmp_path / "does_not_exist.json")
    _mock_token(requests_mock)
    requests_mock.post(WATCH_URL, json={"watch": [{"channel_id": "new", "status": "success"}]})

    main(
        [
            "--base-url",
            "https://example.com",
            "--watch-api-base-url",
            WATCH_API_BASE_URL,
            "--token",
            "test-token",
            "--yes",
        ]
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1
    assert watch_calls[0].method == "POST"
