"""src/sync_engine/zoho_watch_channel.py の単体テスト。

`renew_zoho_watch_channel()`（Vercel Cronからの自動延長のコアロジック）を中心に、
channel_idの解決（引数優先→環境変数フォールバック→未設定時の明確なエラー）・
notify_urlの解決・Zoho API呼び出し失敗時の伝播を検証する。実際のZoho本番APIへは
一切到達させない（requests_mock）。
"""

from __future__ import annotations

import pytest

from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError
from src.sync_engine.zoho_watch_channel import (
    ZohoWatchChannelNotConfiguredError,
    renew_zoho_watch_channel,
)

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
    monkeypatch.delenv("ZOHO_WATCH_CHANNEL_ID", raising=False)
    monkeypatch.delenv("ZOHO_WEBHOOK_BASE_URL", raising=False)


def _mock_token(requests_mock) -> None:
    requests_mock.post(TOKEN_URL, json={"access_token": "access-token-1", "expires_in": 3600})


@pytest.fixture
def client() -> HttpZohoClient:
    return HttpZohoClient()


# --- 正常系: 延長成功 -------------------------------------------------------------------------


def test_renew_watch_channel_puts_using_explicit_channel_id_and_notify_url(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})

    result = renew_zoho_watch_channel(
        client,
        channel_id="123",
        notify_url="https://example.com/api/webhooks/zoho",
        token="secret-token",
        watch_api_base_url=WATCH_API_BASE_URL,
    )

    assert result["channel_id"] == "123"
    assert "channel_expiry" in result
    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1
    assert watch_calls[0].method == "PUT"
    sent = watch_calls[0].json()["watch"][0]
    assert sent["channel_id"] == "123"
    assert sent["events"] == ["Deals.all"]
    assert sent["notify_url"] == "https://example.com/api/webhooks/zoho"
    assert sent["token"] == "secret-token"


def test_renew_watch_channel_falls_back_to_env_var_channel_id(
    requests_mock, client: HttpZohoClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZOHO_WATCH_CHANNEL_ID", "999")
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "999", "status": "success"}]})

    result = renew_zoho_watch_channel(
        client,
        notify_url="https://example.com/api/webhooks/zoho",
        watch_api_base_url=WATCH_API_BASE_URL,
    )

    assert result["channel_id"] == "999"
    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert watch_calls[0].json()["watch"][0]["channel_id"] == "999"


def test_renew_watch_channel_builds_notify_url_from_base_url_env_var(
    requests_mock, client: HttpZohoClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZOHO_WEBHOOK_BASE_URL", "https://crm-sfa-integration.vercel.app")
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"channel_id": "123", "status": "success"}]})

    renew_zoho_watch_channel(client, channel_id="123", watch_api_base_url=WATCH_API_BASE_URL)

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    sent = watch_calls[0].json()["watch"][0]
    assert sent["notify_url"] == "https://crm-sfa-integration.vercel.app/api/webhooks/zoho"


# --- 異常系: channel_id/notify_urlが解決できない --------------------------------------------


def test_renew_watch_channel_raises_when_channel_id_not_configured(
    requests_mock, client: HttpZohoClient
) -> None:
    """channel_id引数も環境変数ZOHO_WATCH_CHANNEL_IDも無い場合、Zoho APIへは一切到達させず
    明確なエラーを送出する（『成功したように見えるno-op』にしない）。"""
    with pytest.raises(ZohoWatchChannelNotConfiguredError, match="ZOHO_WATCH_CHANNEL_ID"):
        renew_zoho_watch_channel(
            client,
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )

    assert len(requests_mock.request_history) == 0


def test_renew_watch_channel_raises_when_notify_url_not_configured(
    requests_mock, client: HttpZohoClient
) -> None:
    with pytest.raises(ZohoWatchChannelNotConfiguredError, match="ZOHO_WEBHOOK_BASE_URL"):
        renew_zoho_watch_channel(client, channel_id="123", watch_api_base_url=WATCH_API_BASE_URL)

    assert len(requests_mock.request_history) == 0


# --- 異常系: Zoho API呼び出し自体の失敗 ------------------------------------------------------


def test_renew_watch_channel_propagates_zoho_api_error_on_http_failure(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, status_code=400, json={"message": "invalid request"})

    with pytest.raises(ZohoApiError):
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )


def test_renew_watch_channel_propagates_zoho_api_error_on_non_success_entry(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(
        WATCH_URL,
        json={"watch": [{"status": "error", "code": "INVALID_DATA", "message": "bad channel_id"}]},
    )

    with pytest.raises(ZohoApiError):
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )


# --- BLOCKER1: エラーメッセージへのtoken漏洩防止 ----------------------------------------------


def test_non_success_entry_error_message_redacts_echoed_back_token(
    requests_mock, client: HttpZohoClient
) -> None:
    """Zohoが2xxかつ`watch`エントリの`status`が非successの場合でも、そのエントリに
    送信したtoken（実体はZOHO_WEBHOOK_SECRET）がそのままエコーバックされて含まれていれば、
    ZohoApiErrorのメッセージへは伏せた値のみを含めること（生の値を含めない）。"""
    _mock_token(requests_mock)
    real_secret = "super-secret-webhook-token-value"
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "error",
                    "code": "INVALID_DATA",
                    "channel_id": "123",
                    "token": real_secret,
                }
            ]
        },
    )

    with pytest.raises(ZohoApiError) as exc_info:
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            token=real_secret,
            watch_api_base_url=WATCH_API_BASE_URL,
        )

    assert real_secret not in str(exc_info.value)
    assert "***REDACTED***" in str(exc_info.value)


# --- BLOCKER2: 確認できるwatchエントリが無い応答をno-op成功にしない ----------------------------


def test_raises_when_response_has_no_watch_key(requests_mock, client: HttpZohoClient) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"someOtherKey": "whatever"})

    with pytest.raises(ZohoApiError):
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )


def test_raises_when_watch_array_is_empty(requests_mock, client: HttpZohoClient) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": []})

    with pytest.raises(ZohoApiError):
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )


def test_raises_when_no_watch_entry_confirms_requested_channel_id(
    requests_mock, client: HttpZohoClient
) -> None:
    """statusはsuccessだが、要求したchannel_idと一致するエントリが1件も無い場合も
    確認できたとはみなさずエラーとする。"""
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": [{"status": "success", "channel_id": "other-id"}]})

    with pytest.raises(ZohoApiError):
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )


# --- BLOCKER3: 想定外のレスポンス形（型不一致・非JSON）を未処理例外のまま漏らさない ------------


def test_raises_zoho_api_error_when_watch_entry_is_not_a_dict(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json={"watch": ["not-a-dict"]})

    with pytest.raises(ZohoApiError):
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )


def test_raises_zoho_api_error_when_response_body_is_not_json(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, status_code=200, text="this is not json")

    with pytest.raises(ZohoApiError):
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )


def test_raises_zoho_api_error_when_response_body_is_a_bare_json_array(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(WATCH_URL, json=["unexpected", "shape"])

    with pytest.raises(ZohoApiError):
        renew_zoho_watch_channel(
            client,
            channel_id="123",
            notify_url="https://example.com/api/webhooks/zoho",
            watch_api_base_url=WATCH_API_BASE_URL,
        )
