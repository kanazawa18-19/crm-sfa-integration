"""src/sync_engine/zoho_watch_channel.py の単体テスト。

`renew_zoho_watch_channel()`（Vercel Cronからの自動延長のコアロジック）を中心に、
channel_idの解決（引数優先→環境変数フォールバック→未設定時の明確なエラー）・
notify_urlの解決・Zoho API呼び出し失敗時の伝播を検証する。実際のZoho本番APIへは
一切到達させない（requests_mock）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.sync_engine.clients.zoho_client import HttpZohoClient, ZohoApiError
from src.sync_engine.zoho_watch_channel import (
    CRON_RENEWAL_EXPIRY_HOURS,
    DEFAULT_EXPIRY_DAYS,
    DEFAULT_MODULES,
    ZohoWatchChannelNotConfiguredError,
    build_watch_payload,
    register_or_renew_watch,
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


# --- build_watch_payload(): 複数モジュールを1つのwatchエントリへ束ねる -------------------------


def test_build_watch_payload_with_multiple_modules_produces_flat_events_array() -> None:
    """`modules`に複数モジュールを渡した場合、`events`はモジュールごとに別エントリを作らず、
    Zoho公式ドキュメント通りのフラットな`"{module}.all"`文字列配列になること。"""
    payload = build_watch_payload(
        channel_id="123",
        modules=DEFAULT_MODULES,
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token=None,
    )

    assert payload == {
        "watch": [
            {
                "channel_id": "123",
                "events": [
                    "Deals.all",
                    "CustomModule3.all",
                    "CustomModule2.all",
                    "Accounts.all",
                    "Contacts.all",
                    "Products.all",
                ],
                "channel_expiry": "2026-08-19T09:00:00+00:00",
                "notify_url": "https://example.com/api/webhooks/zoho",
            }
        ]
    }
    # モジュール数が増えても、watchエントリ自体は1件のまま（モジュールごとに別チャンネルを
    # 作らない設計であることを確認する）。
    assert len(payload["watch"]) == 1


# --- 正常系: 延長成功 -------------------------------------------------------------------------


def test_renew_watch_channel_puts_using_explicit_channel_id_and_notify_url(
    requests_mock, client: HttpZohoClient
) -> None:
    _mock_token(requests_mock)
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {"events": [{"channel_id": "123"}]},
                }
            ]
        },
    )

    result = renew_zoho_watch_channel(
        client,
        channel_id="123",
        modules=["Deals"],
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


def test_renew_watch_channel_defaults_to_all_six_modules(
    requests_mock, client: HttpZohoClient
) -> None:
    """`modules`省略時は、フィールドマッピングでカバー済みの6モジュール全て
    （`DEFAULT_MODULES`）を1つのwatchチャンネルでまとめて延長対象とする。"""
    assert DEFAULT_MODULES == [
        "Deals",
        "CustomModule3",
        "CustomModule2",
        "Accounts",
        "Contacts",
        "Products",
    ]
    _mock_token(requests_mock)
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {"events": [{"channel_id": "123"}]},
                }
            ]
        },
    )

    renew_zoho_watch_channel(
        client,
        channel_id="123",
        notify_url="https://example.com/api/webhooks/zoho",
        watch_api_base_url=WATCH_API_BASE_URL,
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    sent = watch_calls[0].json()["watch"][0]
    assert sent["events"] == [f"{module}.all" for module in DEFAULT_MODULES]


def test_renew_watch_channel_falls_back_to_env_var_channel_id(
    requests_mock, client: HttpZohoClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZOHO_WATCH_CHANNEL_ID", "999")
    _mock_token(requests_mock)
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {"events": [{"channel_id": "999"}]},
                }
            ]
        },
    )

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
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {"events": [{"channel_id": "123"}]},
                }
            ]
        },
    )

    renew_zoho_watch_channel(client, channel_id="123", watch_api_base_url=WATCH_API_BASE_URL)

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    sent = watch_calls[0].json()["watch"][0]
    assert sent["notify_url"] == "https://crm-sfa-integration.vercel.app/api/webhooks/zoho"


# --- cron自動延長の安全マージン（既定expiry_days）----------------------------------------------
# Vercel Hobbyプランではcronが1日1回しか実行されないため、renew_zoho_watch_channel()は
# 既定でZoho上限の24hいっぱいではなく21h先のchannel_expiryを要求する（3時間の安全マージン）。
# docs/zoho_webhook_activation_note.md参照。


def test_renew_watch_channel_default_expiry_requests_less_than_full_day_margin(
    requests_mock, client: HttpZohoClient
) -> None:
    """expiry_days未指定（cronの実運用と同じ呼び出し方）の場合、送信されるchannel_expiryが
    Zoho上限の24hぴったりではなく、CRON_RENEWAL_EXPIRY_HOURS（21h）分だけ先の値になること
    （＝24hとの差＝安全マージンが確保されていること）を、送信ペイロードの実際の値で検証する。
    """
    _mock_token(requests_mock)
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {"events": [{"channel_id": "123"}]},
                }
            ]
        },
    )

    before = datetime.now(timezone.utc)
    result = renew_zoho_watch_channel(
        client,
        channel_id="123",
        notify_url="https://example.com/api/webhooks/zoho",
        watch_api_base_url=WATCH_API_BASE_URL,
    )
    after = datetime.now(timezone.utc)

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    sent_expiry = datetime.fromisoformat(watch_calls[0].json()["watch"][0]["channel_expiry"])
    assert sent_expiry == datetime.fromisoformat(result["channel_expiry"])

    # 上限（24h）いっぱいは要求しない（安全マージンが無いと、cronの遅延・失敗1回で
    # チャンネルが失効しうる）。
    assert sent_expiry < before + timedelta(days=1)
    # CRON_RENEWAL_EXPIRY_HOURS（21h）分だけ先の値であること（実行時間のブレを許容して幅を持たせる）。
    assert sent_expiry >= before + timedelta(hours=CRON_RENEWAL_EXPIRY_HOURS) - timedelta(seconds=5)
    assert sent_expiry <= after + timedelta(hours=CRON_RENEWAL_EXPIRY_HOURS) + timedelta(seconds=5)


def test_renew_watch_channel_explicit_expiry_days_overrides_cron_default(
    requests_mock, client: HttpZohoClient
) -> None:
    """呼び出し側が明示的にexpiry_daysを指定した場合（CLI側が`channel_id`/`notify_url`を渡して
    renew_zoho_watch_channel()を利用するケースなど）は、cron既定の21hマージンではなく
    指定した値がそのまま使われること。"""
    _mock_token(requests_mock)
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {"events": [{"channel_id": "123"}]},
                }
            ]
        },
    )

    before = datetime.now(timezone.utc)
    renew_zoho_watch_channel(
        client,
        channel_id="123",
        notify_url="https://example.com/api/webhooks/zoho",
        expiry_days=DEFAULT_EXPIRY_DAYS,
        watch_api_base_url=WATCH_API_BASE_URL,
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    sent_expiry = datetime.fromisoformat(watch_calls[0].json()["watch"][0]["channel_expiry"])
    assert sent_expiry >= before + timedelta(hours=CRON_RENEWAL_EXPIRY_HOURS)


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


# --- 複数モジュールをまとめた1つのwatchエントリに対する応答確認（events配列が複数件の場合） -----


def test_confirms_channel_id_from_response_with_one_event_entry_per_module(
    requests_mock, client: HttpZohoClient
) -> None:
    """`events`配列に6モジュール分の操作を含む1つのwatchエントリを登録した場合、Zoho応答の
    `details.events`側も6モジュール分（各要素が同じchannel_idを持つ）のエントリを返してくる
    ことを想定した、より実際のレスポンス形に近いケース。`_confirmed_channel_ids()`が
    複数イベントエントリの中からいずれか1つでもchannel_idが一致すれば確認済みとみなす
    実装であることを、6モジュール分のレスポンスを使って確認する。

    各イベント要素が`module`フィールドで6モジュール全てを個別に特定できる（1つのイベント
    エントリが6モジュール分を代表しているわけではない）ため、WARN1で追加した
    全モジュール確認チェックも素通りすること（本当に確認できているケース）を兼ねて検証する。
    """
    _mock_token(requests_mock)
    payload = build_watch_payload(
        channel_id="123",
        modules=DEFAULT_MODULES,
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token=None,
    )
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {
                        "events": [
                            {
                                "module": module,
                                "channel_id": "123",
                                "resource_uri": f"https://www.zohoapis.jp/crm/v3/{module}",
                            }
                            for module in DEFAULT_MODULES
                        ]
                    },
                }
            ]
        },
    )

    result = register_or_renew_watch(
        client, watch_api_base_url=WATCH_API_BASE_URL, payload=payload, is_renewal=True
    )

    watch_calls = [req for req in requests_mock.request_history if req.url == WATCH_URL]
    assert len(watch_calls) == 1
    assert watch_calls[0].json()["watch"][0]["events"] == [f"{m}.all" for m in DEFAULT_MODULES]
    assert result["watch"][0]["status"] == "success"


# --- WARN1: 要求した全モジュールが確認できたことまで検証する ------------------------------------


def test_raises_when_one_of_six_requested_modules_is_missing_from_response(
    requests_mock, client: HttpZohoClient
) -> None:
    """6モジュールを1つのwatchエントリへまとめて要求したのに、Zoho応答の
    `details.events`には5モジュール分のイベントしか含まれていない（1モジュールが
    静かに登録に失敗した）場合、channel_id自体は一致しているため以前の実装では
    「成功」扱いになっていたが、WARN1対策後はZohoApiErrorを送出し、かつどのモジュールが
    確認できなかったかをエラーメッセージへ明記すること。"""
    _mock_token(requests_mock)
    missing_module = "Products"
    confirmed_modules = [m for m in DEFAULT_MODULES if m != missing_module]
    payload = build_watch_payload(
        channel_id="123",
        modules=DEFAULT_MODULES,
        notify_url="https://example.com/api/webhooks/zoho",
        channel_expiry="2026-08-19T09:00:00+00:00",
        token=None,
    )
    requests_mock.put(
        WATCH_URL,
        json={
            "watch": [
                {
                    "status": "success",
                    "details": {
                        "events": [
                            {
                                "module": module,
                                "channel_id": "123",
                                "resource_uri": f"https://www.zohoapis.jp/crm/v3/{module}",
                            }
                            for module in confirmed_modules
                        ]
                    },
                }
            ]
        },
    )

    with pytest.raises(ZohoApiError, match=missing_module):
        register_or_renew_watch(
            client, watch_api_base_url=WATCH_API_BASE_URL, payload=payload, is_renewal=True
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
